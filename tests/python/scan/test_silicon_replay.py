"""Each scan flow's exported patterns, replayed on the scanned sky130 netlist with the
PDK's own cell models (scan_replay), unload what FaultFlow expects: what a tester
would see on a good chip. FaultFlow's simulators are the reference everywhere else;
this is where the patterns meet the real cells.

The chips are sky130 netlists before scan: clk 2, d0 4, d1 5 unless a test says
otherwise."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from faultflow.config import load_config
from scan_replay import (
    replay_compacted_on_cells,
    replay_compressed_on_cells,
    replay_on_cells,
)
from warptap_helpers import skip_unless_warptap

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "chip"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    outputs = ("Q", "X", "Y", "dout")
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _gate(kind: str, a: int, b: int, x: int) -> dict[str, Any]:
    return _cell(f"sky130_fd_sc_hd__{kind}_1", A=a, B=b, X=x)


def _chip(
    cells: dict[str, Any], inputs: dict[str, int], outputs: dict[str, int]
) -> dict[str, Any]:
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in inputs.items()},
        **{n: {"direction": "output", "bits": [b]} for n, b in outputs.items()},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


def _ring() -> dict[str, Any]:
    """Three flops in a ring through logic: r0 = d0 ^ r2, r1 = r0 & d1,
    r2 = r1 | d0; y = r2 ^ r0."""
    cells = {
        "g_x": _gate("xor2", 4, 19, 14),
        "r0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_a": _gate("and2", 15, 5, 16),
        "r1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 17, 4, 18),
        "r2": _cell(DFXTP, CLK=2, D=18, Q=19),
        "g_y": _gate("xor2", 19, 15, 20),
    }
    return _chip(cells, {"clk": 2, "d0": 4, "d1": 5}, {"y": 20})


def _two_domains() -> dict[str, Any]:
    """clk_a 2 clocks a0 and a1, clk_b 3 clocks b0 and b1; a1 feeds b0."""
    cells = {
        "g_x": _gate("xor2", 4, 5, 14),
        "a0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_a": _gate("and2", 15, 5, 16),
        "a1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 17, 4, 18),
        "b0": _cell(DFXTP, CLK=3, D=18, Q=19),
        "g_b": _gate("xor2", 19, 5, 20),
        "b1": _cell(DFXTP, CLK=3, D=20, Q=21),
        "g_y": _gate("and2", 21, 15, 22),
    }
    inputs = {"clk_a": 2, "clk_b": 3, "d0": 4, "d1": 5}
    return _chip(cells, inputs, {"y": 22})


def _with_memory() -> dict[str, Any]:
    """u_mem, a blackbox (din 6, dout 13), feeds r0: r0 = dout & d0,
    r1 = r0 ^ d1, r2 = d0 | d1; y = r1 & r2."""
    cells = {
        "u_mem": _cell("sram_like", din=6, dout=13),
        "g_m": _gate("and2", 13, 4, 14),
        "r0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_x": _gate("xor2", 15, 5, 16),
        "r1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 4, 5, 18),
        "r2": _cell(DFXTP, CLK=2, D=18, Q=19),
        "g_y": _gate("and2", 17, 19, 20),
    }
    inputs = {"clk": 2, "d0": 4, "d1": 5, "mdin": 6}
    return _chip(cells, inputs, {"y": 20})


def _bus_out() -> dict[str, Any]:
    """_ring with a two-bit output y = {r1 & d1, r2 ^ r0}, and no per-bit netnames
    (as Yosys writes a plain bus port)."""
    chip = _ring()
    module = chip["modules"][TOP]
    module["cells"]["g_z"] = _gate("and2", 17, 5, 21)
    module["ports"]["y"] = {"direction": "output", "bits": [20, 21]}
    return chip


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _flow(
    work: Path, chip: dict[str, Any], sections: str, *, compose: tuple[str, ...] = ()
) -> Any:
    """init, scan, scan-check, the `compose` steps (scan-compress, scan-compact)
    and sim --scan on `chip` with `sections`; the config and the exported
    patterns' path. Run in `work`."""
    from faultflow.cli import main

    netlist = work / "chip.json"
    netlist.write_text(json.dumps(chip), encoding="utf-8")
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n{sections}",
        encoding="utf-8",
    )
    patterns = work / "patterns.json"
    steps = [["init"], ["scan"], ["scan-check"]]
    steps += [[step] for step in compose]
    steps.append(["sim", "--scan", "--export-patterns", str(patterns)])
    for step in steps:
        assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    return load_config(ofs, TOP), patterns


def _replay(work: Path, chip: dict[str, Any], sections: str) -> list[dict[str, Any]]:
    """_flow, then every exported pattern must unload on the cells as expected; the
    exported patterns."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(work)
        cfg, patterns = _flow(work, chip, sections)
        assert replay_on_cells(cfg, patterns, work / "replay") == []
    return list(json.loads(patterns.read_text(encoding="utf-8")))


@pytest.mark.integration
def test_launch_on_capture_patterns_unload_on_real_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    """A launch-on-capture pattern pulses the clock twice after the load -- the
    launch, then the capture -- and says so (launch = loc)."""
    exported = _replay(
        tmp_path,
        _ring(),
        "[fault_model]\nmodel = transition\nlaunch = loc\ncollapsing = false\n\n"
        "[scan]\nchains = 1\n",
    )
    assert exported and {p["launch"] for p in exported} == {"loc"}


@pytest.mark.integration
def test_launch_on_shift_patterns_unload_on_real_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    """A launch-on-shift pattern shifts once more after the load, scan enable on,
    with its own scan-in bit per chain (launch_scan_in), then captures."""
    exported = _replay(
        tmp_path,
        _ring(),
        "[fault_model]\nmodel = transition\nlaunch = los\ncollapsing = false\n\n"
        "[scan]\nchains = 1\n",
    )
    assert exported and {p["launch"] for p in exported} == {"los"}
    assert all(set(p["launch_scan_in"]) == {"0"} for p in exported)


@pytest.mark.integration
def test_two_clock_domains_patterns_unload_on_real_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    _replay(
        tmp_path,
        _two_domains(),
        "[clocks]\nports = clk_a, clk_b\n\n[scan]\nchains = 2\n",
    )


@pytest.mark.integration
def test_every_bit_of_an_output_bus_is_compared(
    tmp_path: Path, flow_tools: None
) -> None:
    """Without per-bit netnames, the bus's bits are y[0] and y[1] by position: each
    pattern gives both their values, and both match on the cells."""
    exported = _replay(tmp_path, _bus_out(), "[scan]\nchains = 1\n")
    assert all({"y[0]", "y[1]"} <= set(p["capture_pi_values"]) for p in exported)


def _seed_solver(cfg: Any) -> Callable[[int, dict[str, Any]], int]:
    """Each exported pattern's seed, solved as a tester solves it (warptap), for
    the decompressor the manifest records."""
    from warptap.faultflow_compression import (
        care_bit_rows,
        polynomial_from_manifest,
        solve_pattern_seed,
    )

    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    poly = polynomial_from_manifest(compression)
    rows = care_bit_rows(
        poly, compression["phase_shifter_taps"], int(manifest["max_chain_length"])
    )

    def seed_of(number: int, raw: dict[str, Any]) -> int:
        return int(solve_pattern_seed(raw["load_seqs"], rows, poly.width, number))

    return seed_of


COMPRESSION = "[compression]\nenabled = true\nchannels = 8\n"
COMPACTION = "[compaction]\nenabled = true\nchannels = 2\n"


@pytest.mark.integration
def test_compressed_patterns_load_through_the_decompressor_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    """With scan compression a pattern is one seed's load: the seed, solved as a
    tester solves it, held on the channel bus of the composed chip, the
    decompressor's own cells load the three chains, and every pattern unloads and
    strobes what FaultFlow expects."""
    skip_unless_warptap("warptap.faultflow_compression")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = _flow(
            tmp_path,
            _ring(),
            f"[scan]\nchains = 3\n\n{COMPRESSION}",
            compose=("scan-compress",),
        )
        assert json.loads(patterns.read_text(encoding="utf-8"))
        seed_of = _seed_solver(cfg)
        replayed = replay_compressed_on_cells(cfg, patterns, tmp_path / "r", seed_of)
        assert replayed == []


@pytest.mark.integration
def test_compressed_and_compacted_patterns_replay_on_the_whole_chip(
    tmp_path: Path, flow_tools: None
) -> None:
    """With both, scan-compact composes the decompressor, the core and the
    compactor into one netlist, the core still core_inst: each seed held on the
    compression channels, every pattern's unload, read on the compactor's
    channels, matches on its cells."""
    skip_unless_warptap("warptap.faultflow_compression")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = _flow(
            tmp_path,
            _ring(),
            f"[scan]\nchains = 3\n\n{COMPRESSION}\n{COMPACTION}",
            compose=("scan-compress", "scan-compact"),
        )
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        compaction = manifest["compaction"]
        assert compaction["with_decompressor"] is True
        chip = json.loads(Path(compaction["composed_json"]).read_text("utf-8"))
        module = chip["modules"][compaction["composed_top"]]
        assert {"tdi", "tdo"} <= set(module["ports"])
        assert "lfsr_reg" in module["netnames"]
        assert {f"core_inst__{flop}" for flop in ("r0", "r1", "r2")} <= set(
            module["cells"]
        )
        assert json.loads(patterns.read_text(encoding="utf-8"))
        seed_of = _seed_solver(cfg)
        replayed = replay_compressed_on_cells(cfg, patterns, tmp_path / "r", seed_of)
        assert replayed == []


@pytest.mark.integration
def test_compacted_patterns_unload_through_the_compactor_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    """With scan compaction a tester sees only the channels: the compactor's own
    cells XOR three chains onto two, and every channel bit is the XOR of the
    expected unload bits it reads."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = _flow(
            tmp_path,
            _ring(),
            f"[scan]\nchains = 3\n\n{COMPACTION}",
            compose=("scan-compact",),
        )
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        assert len(manifest["compaction"]["fanout"]) == 2
        assert json.loads(patterns.read_text(encoding="utf-8"))
        assert replay_compacted_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
def test_blackbox_patterns_unload_on_real_cells(
    tmp_path: Path, flow_tools: None
) -> None:
    """u_mem's output is unknown on the cells (a port-only stub): every unload bit
    it can reach is masked, and every other one matches."""
    exported = _replay(
        tmp_path,
        _with_memory(),
        "[blackbox]\ninstances = u_mem\n\n[scan]\nchains = 1\n",
    )
    assert any(p.get("unload_mask") for p in exported)
