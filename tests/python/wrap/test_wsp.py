"""The IEEE 1500 WSP [wrap] control = wsp puts on (faultflow.wrap.wsp), checked against
its behavioural model (faultflow.wrap.wsp_model) cycle by cycle -- by FaultFlow's
simulator on the scanned netlist and by iverilog on its sky130 cells -- and the
rules the model encodes: WRSTN puts WS_BYPASS in force at once; an instruction shifts
in without taking effect until UpdateWR, on WRCK's falling edge; WBY is one bit;
WS_EXTEST's path from WSI to WSO is every boundary cell; an unused code acts as
WS_BYPASS; the boundary cells keep their values while the WIR shifts and updates.

The block: y = a & b, z = a ^ c, no flops; its five boundary cells on two wrapper
chains."""

from __future__ import annotations

import dataclasses
import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.wrap.cell import INPUT, OUTPUT
from faultflow.wrap.wsp_model import (
    OPCODES,
    SERIAL,
    WspCycle,
    WspModel,
    program,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
TOP = "comb3"
WSP_INPUTS = ("wrstn", "selectwir", "shiftwr", "capturewr", "updatewr", "wsi")


def _block() -> dict[str, Any]:
    def cell(kind: str, **conns: int) -> dict[str, Any]:
        return {
            "hide_name": 0,
            "type": kind,
            "parameters": {},
            "attributes": {},
            "port_directions": {p: "output" if p == "X" else "input" for p in conns},
            "connections": {p: [n] for p, n in conns.items()},
        }

    ports = {
        "a": {"direction": "input", "bits": [2]},
        "b": {"direction": "input", "bits": [3]},
        "c": {"direction": "input", "bits": [4]},
        "y": {"direction": "output", "bits": [5]},
        "z": {"direction": "output", "bits": [6]},
    }
    cells = {
        "g_and": cell("sky130_fd_sc_hd__and2_1", A=2, B=3, X=5),
        "g_xor": cell("sky130_fd_sc_hd__xor2_1", A=2, B=4, X=6),
    }
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {},
            }
        }
    }


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


@dataclasses.dataclass
class Wrapped:
    cfg: Any
    manifest: dict[str, Any]
    module: dict[str, Any]
    sides: list[str]  # the boundary cells', in serial order (WSI first)
    labels: list[str]
    inputs: list[str]  # the scanned block's input port bits


def _wrap(work: Path) -> Wrapped:
    """init and scan (which wraps first) with [wrap] control = wsp, then scan-check;
    the boundary cells in serial order: wrapper chain 0's, then 1's."""
    from faultflow.cli import main

    netlist = work / "comb3.json"
    netlist.write_text(json.dumps(_block()), encoding="utf-8")
    ofs = work / "comb3.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n[wrap]\nenabled = true\ncontrol = wsp\n\n"
        "[scan]\nchains = 1\nwrapper_chains = 2\n",
        encoding="utf-8",
    )
    for step in (["init"], ["scan"], ["scan-check"]):
        assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    cfg = load_config(ofs, TOP)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    for key in ("generic_json", "sky130_verilog"):  # the run's paths are relative
        manifest[key] = str(Path(manifest[key]).resolve())
    data = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    module = data["modules"][TOP]
    by_ff = {cell["ff"]: cell for cell in manifest["wrapper"]["cells"]}
    ordered = [
        by_ff[name]
        for chain in manifest["chains"]
        if chain["kind"] == "wrapper"
        for name in chain["cells"]
    ]
    inputs = [
        name for name, port in module["ports"].items() if port["direction"] == "input"
    ]
    return Wrapped(
        cfg,
        manifest,
        module,
        [cell["side"] for cell in ordered],
        [cell["label"] for cell in ordered],
        inputs,
    )


@dataclasses.dataclass(frozen=True)
class Cycle:
    """A tester cycle: the WSP's signals and the block's data inputs."""

    wsp: WspCycle
    ports: dict[str, int]


def _model(wrapped: Wrapped, cycles: list[Cycle]) -> list[dict[str, int | None]]:
    """What the model says at each cycle's strobe: WSO, y and z (None: unknown)."""
    model = WspModel(wrapped.sides)
    found: list[dict[str, int | None]] = []
    for cycle in cycles:
        # The core sees the input cells' outputs; the output cells read the core.
        cfi: list[int | None] = [
            cycle.ports[label] if side == INPUT else None
            for side, label in zip(wrapped.sides, wrapped.labels)
        ]
        bare = dataclasses.replace(cycle.wsp, cfi=tuple(cfi))
        seen = model.outputs(bare)
        core = {
            label: seen[k]
            for k, (side, label) in enumerate(zip(wrapped.sides, wrapped.labels))
            if side == INPUT
        }

        def both(f: Any, x: int | None, w: int | None) -> int | None:
            return None if x is None or w is None else f(x, w)

        driven = {
            "y": both(lambda x, w: x & w, core["a"], core["b"]),
            "z": both(lambda x, w: x ^ w, core["a"], core["c"]),
        }
        cfi = [
            driven[label] if side == OUTPUT else value
            for side, label, value in zip(wrapped.sides, wrapped.labels, cfi)
        ]
        full = dataclasses.replace(cycle.wsp, cfi=tuple(cfi))
        out = model.outputs(full)
        ports = {
            label: out[k]
            for k, (side, label) in enumerate(zip(wrapped.sides, wrapped.labels))
            if side == OUTPUT
        }
        # WSO comes from the WBR: a serial instruction, the WIR deselected.
        from_wbr = model.instruction in SERIAL and not cycle.wsp.selectwir
        found.append({"wso": model.step(full), "y": ports["y"], "z": ports["z"]})
        found[-1]["_from_wbr"] = int(from_wbr)
    return found


def _values(cycle: Cycle, clock: int) -> dict[str, int]:
    values = {name: getattr(cycle.wsp, name) for name in WSP_INPUTS}
    values.update(cycle.ports)
    values["wrck"] = clock
    return values


def _faultflow(wrapped: Wrapped, cycles: list[Cycle]) -> list[dict[str, int]]:
    """FaultFlow's simulator on the scanned netlist: WSO, y and z at each strobe."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    steps: list[list[bool]] = []
    sample: list[bool] = []
    for cycle in cycles:
        levels = [0, 1, 0] if cycle.wsp.pulse else [0]
        for k, clock in enumerate(levels):
            values = _values(cycle, clock)
            steps.append([bool(values.get(name, 0)) for name in wrapped.inputs])
            sample.append(k == 0)
    result = core.simulate_sequence_faults(
        str(wrapped.manifest["generic_json"]),
        str(CELL_MAP_PATH),
        wrapped.inputs,
        steps,
        sample,
        ["wso", "y", "z"],
        [],
    )
    return [
        {"wso": int(row[0]), "y": int(row[1]), "z": int(row[2])}
        for row in result["golden"]
    ]


def _iverilog(
    wrapped: Wrapped, cycles: list[Cycle], work: Path
) -> list[dict[str, str]]:
    """iverilog on the scanned block's sky130 cells: WSO, y and z at each strobe,
    WRCK rising then falling within the cycle."""
    lines = ["`timescale 1ns/1ps", "module tb;"]
    lines += [f"  reg \\{name} = 0;" for name in wrapped.inputs]
    lines += ["  wire \\wso , \\y , \\z ;"]
    outputs = [
        name
        for name, port in wrapped.module["ports"].items()
        if port["direction"] == "output"
    ]
    lines += [f"  wire \\{name} ;" for name in outputs if name not in ("wso", "y", "z")]
    conns = ", ".join(f".\\{n} (\\{n} )" for n in [*wrapped.inputs, *outputs])
    lines += [f"  {TOP} dut({conns});", "  initial begin"]
    for cycle in cycles:
        for name, value in _values(cycle, 0).items():
            lines.append(f"    \\{name} = {value};")
        lines += ["    #4;", '    $display("S %b %b %b", \\wso , \\y , \\z );']
        if cycle.wsp.pulse:
            lines += ["    \\wrck = 1;", "    #3;", "    \\wrck = 0;", "    #3;"]
        else:
            lines.append("    #6;")
    lines += ["    $finish;", "  end", "endmodule"]
    bench = work / "wsp_tb.v"
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sim = work / "wsp_tb.vvp"
    subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-DFUNCTIONAL",
            "-o",
            str(sim),
            str(bench),
            str(Path(wrapped.manifest["sky130_verilog"]).resolve()),
            str(MODELS),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    shown = subprocess.run(
        ["vvp", "-n", str(sim)], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return [
        dict(zip(("wso", "y", "z"), line.split()[1:]))
        for line in shown
        if line.startswith("S ")
    ]


def _random_ops(rng: random.Random, count: int) -> list[Cycle]:
    """Instructions put in force and random WSP operations, the data inputs random."""

    def ports() -> dict[str, int]:
        return {name: rng.randint(0, 1) for name in ("a", "b", "c")}

    cycles: list[Cycle] = [Cycle(c, ports()) for c in program("WS_BYPASS")]
    for _ in range(count):
        kind = rng.choice(
            ("program", "shift_dr", "capture_dr", "shift_ir", "update_ir", "idle")
        )
        if kind == "program":
            # The serial instructions most, which shift the WBR on the WSP.
            serial = (OPCODES["WS_EXTEST"], OPCODES["WS_INTEST"]) * 3
            code = rng.choice([*serial, *OPCODES.values(), 0b101, 0b110, 0b111])
            cycles += [Cycle(c, ports()) for c in program(code)]
        elif kind == "shift_dr":
            for _ in range(rng.randint(1, 12)):
                wsp = WspCycle(shiftwr=1, wsi=rng.randint(0, 1))
                cycles.append(Cycle(wsp, ports()))
        elif kind == "capture_dr":
            cycles.append(Cycle(WspCycle(capturewr=1), ports()))
        elif kind == "shift_ir":
            for _ in range(rng.randint(1, 3)):
                wsp = WspCycle(selectwir=1, shiftwr=1, wsi=rng.randint(0, 1))
                cycles.append(Cycle(wsp, ports()))
        elif kind == "update_ir":
            cycles.append(Cycle(WspCycle(selectwir=1, updatewr=1), ports()))
        else:
            cycles.append(Cycle(WspCycle(pulse=rng.random() < 0.5), ports()))
    return cycles


def _agree(
    model: list[dict[str, int | None]], other: list[dict[str, Any]], who: str
) -> int:
    """How many known model values `other` matched; fails on a mismatch."""
    assert len(model) == len(other), who
    checked = 0
    for number, (want, have) in enumerate(zip(model, other)):
        for signal, value in want.items():
            if value is None or signal.startswith("_"):
                continue
            assert str(have[signal]) == str(value), (who, number, signal, want, have)
            checked += 1
    return checked


@pytest.mark.integration
def test_the_wsp_behaves_as_its_model(tmp_path: Path, flow_tools: None) -> None:
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        wrapped = _wrap(tmp_path)
    assert wrapped.sides.count(INPUT) == 3 and wrapped.sides.count(OUTPUT) == 2
    cycles = _random_ops(random.Random(1500), 200)
    model = _model(wrapped, cycles)
    # The serial path through every boundary cell is what most of it checks.
    from_wbr = [row for row in model if row["_from_wbr"] and row["wso"] is not None]
    assert len(from_wbr) > 50
    assert _agree(model, _faultflow(wrapped, cycles), "faultflow") > 300
    assert _agree(model, _iverilog(wrapped, cycles, tmp_path), "iverilog") > 300


@pytest.mark.integration
def test_the_wsp_on_the_block_and_its_chains(tmp_path: Path, flow_tools: None) -> None:
    """The WSP's ports in place of the mode pins, its seven flops off the scan
    chains, the boundary cells' flops on WRCK, the manifest's record of it -- and
    its functional scan test, every pattern loading WS_BYPASS first
    (faultflow.wrap.wsp_setup)."""
    from faultflow.cli import main
    from faultflow.scan.setup import setup_to_list
    from faultflow.wrap.wsp import WSP_ATTR
    from faultflow.wrap.wsp_setup import mode_program
    from wrap_flow import manifest_validator

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        wrapped = _wrap(tmp_path)
        ofs = tmp_path / "comb3.ofs"
        patterns = tmp_path / "functional_patterns.json"
        run = ["sim", "--scan", "--export-patterns", str(patterns), "--top", TOP]
        assert main([*run, "-c", str(ofs)]) == 0
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    bypass = setup_to_list(mode_program(wrapped.manifest["wrapper"], "functional"))
    assert exported and all(pattern["setup"] == bypass for pattern in exported)
    ports = wrapped.module["ports"]
    wsp_ports = {"wrck", "wrstn", "selectwir", "shiftwr", "capturewr", "updatewr"}
    assert wsp_ports | {"wsi"} <= {
        n for n, p in ports.items() if p["direction"] == "input"
    }
    assert ports["wso"]["direction"] == "output"
    assert not {"wbr_intest", "wbr_extest"} & set(ports)
    manifest = wrapped.manifest
    manifest_validator().validate(manifest)
    wsp_flops = sorted(
        name
        for name, cell in wrapped.module["cells"].items()
        if WSP_ATTR in cell["attributes"] and "Q" in cell["connections"]
    )
    assert len(wsp_flops) == 7  # the WIR's 3 + 3, WBY
    assert (
        sorted(
            ff["instance"] for ff in manifest["ineligible_ffs"] if ff["reason"] == "wsp"
        )
        == wsp_flops
    )
    record = manifest["wrapper"]
    assert record["control"] == "wsp" and "intest" not in record
    assert record["clock"]["port"] == "wrck"
    wrck = ports["wrck"]["bits"][0]
    for cell in record["cells"]:
        assert wrapped.module["cells"][cell["ff"]]["connections"]["CLK"] == [wrck]
    assert record["wsp"]["opcodes"] == OPCODES


@pytest.mark.unit
def test_the_models_rules() -> None:
    sides = [INPUT, INPUT, INPUT, OUTPUT, OUTPUT]
    model = WspModel(sides)
    # WRSTN: WS_BYPASS at once, WBY cleared, no clock needed.
    for cycle in program("WP_INTEST"):
        model.step(cycle)
    assert model.instruction == "WP_INTEST"
    assert model.wso(WspCycle(wrstn=0, pulse=False)) == 0
    assert model.instruction == "WS_BYPASS"
    # WBY is one bit: WSI comes out at WSO one cycle later.
    seen = [model.step(WspCycle(shiftwr=1, wsi=bit)) for bit in (1, 0, 1, 1)]
    assert seen[1:] == [1, 0, 1]
    # WS_EXTEST: WSI to WSO through every cell, five shifts.
    for cycle in program("WS_EXTEST"):
        model.step(cycle)
    bits = [1, 0, 0, 1, 1, 0, 1, 0, 1, 1]
    out = [model.step(WspCycle(shiftwr=1, wsi=bit)) for bit in bits]
    assert out[5:] == bits[:5]
    # The cells keep their values while the WIR shifts and updates.
    kept = list(model.wbr)
    for cycle in program("WS_EXTEST")[1:]:
        model.step(cycle)
    assert model.wbr == kept
    # A shifted code takes effect only at its update.
    for cycle in program("WP_EXTEST")[:-2]:
        model.step(cycle)
    assert model.instruction == "WS_BYPASS"  # the reset, and no update yet
    model.step(WspCycle(selectwir=1, updatewr=1))
    assert model.instruction == "WP_EXTEST"
    # An unused code acts as WS_BYPASS.
    for code in (0b101, 0b110, 0b111):
        for cycle in program(code):
            model.step(cycle)
        assert model.instruction == "WS_BYPASS"


@pytest.mark.unit
def test_control_wsp_in_the_config(tmp_path: Path) -> None:
    from faultflow.config import ConfigError

    ofs = tmp_path / "w.ofs"
    ofs.write_text("[wrap]\nenabled = true\ncontrol = wsp\n", encoding="utf-8")
    assert load_config(ofs, TOP).wrap.control == "wsp"
    ofs.write_text("[wrap]\ncontrol = wsp\nclock = clk\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="runs on the WSP's WRCK"):
        load_config(ofs, TOP)
    ofs.write_text("[wrap]\ncontrol = tap\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="pins or wsp"):
        load_config(ofs, TOP)


@pytest.mark.unit
def test_wrapping_with_a_wsp_refuses_a_clock_or_a_taken_port() -> None:
    from faultflow.wrap.block import WrapOptions, wrap_block
    from faultflow.wrap.errors import WrapError

    cell_map = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
    result = wrap_block(_block(), TOP, cell_map, WrapOptions(control="wsp"))
    assert result.clock == "wrck"
    with pytest.raises(WrapError, match="runs on WRCK"):
        wrap_block(_block(), TOP, cell_map, WrapOptions(clock="a", control="wsp"))
    taken = _block()
    taken["modules"][TOP]["ports"]["wsi"] = {"direction": "input", "bits": [9]}
    with pytest.raises(WrapError, match="wsi is already a port"):
        wrap_block(taken, TOP, cell_map, WrapOptions(control="wsp"))
