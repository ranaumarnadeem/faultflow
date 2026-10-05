"""The wrapper boundary cell (faultflow.wrap.cell): its three sky130 cells do what its
spec says, row by row, in FaultFlow's simulator and in iverilog on the PDK's own cell
models; and FaultFlow detects a fault in them exactly when the cells do.

Each test netlist is one cell on port bit "p": inputs clk, cfi (the cell's
functional input), intest and extest; outputs out (its functional output) and q (its
flop's)."""

from __future__ import annotations

import copy
import itertools
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.wrap.cell import INPUT, SIDES, CellNets, add_cell, spec

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
INPUTS = ["clk", "cfi", "intest", "extest"]
OUTPUTS = ["out", "q"]
NETS = {"clk": 2, "cfi": 3, "intest": 4, "extest": 5, "out": 6, "q": 7, "cfo": 8}
# A row: intest, extest, cfi, and the flop's value before the clock edge.
ROWS = list(itertools.product((0, 1), repeat=4))
# A row's steps: load the flop in the functional mode, apply the row (sampled: out,
# and q before the edge), the edge, then low again (sampled: q after the edge).
SAMPLED = (2, 4)


def _one_cell(side: str) -> dict[str, Any]:
    hold, safe = ("intest", "extest") if side == INPUT else ("extest", "intest")
    nets = CellNets(
        cfi=NETS["cfi"],
        out=NETS["out"],
        cfo=NETS["cfo"],
        q=NETS["q"],
        hold=NETS[hold],
        safe=NETS[safe],
        clock=NETS["clk"],
    )
    cells: dict[str, Any] = {}
    add_cell(cells, side, "p", 0, nets)
    ports = {name: {"direction": "input", "bits": [NETS[name]]} for name in INPUTS}
    for name in OUTPUTS:
        ports[name] = {"direction": "output", "bits": [NETS[name]]}
    netnames = {
        name: {"hide_name": 0, "bits": [net], "attributes": {}}
        for name, net in NETS.items()
    }
    module = {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": netnames,
    }
    return {"modules": {"wcell": module}}


def _steps(row: tuple[int, ...]) -> list[dict[str, bool]]:
    intest, extest, cfi, q = row
    load = {"cfi": bool(q), "intest": False, "extest": False}
    apply = {"cfi": bool(cfi), "intest": bool(intest), "extest": bool(extest)}
    return [
        {**load, "clk": False},
        {**load, "clk": True},
        {**apply, "clk": False},
        {**apply, "clk": True},
        {**apply, "clk": False},
    ]


@pytest.fixture
def tools() -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool} on PATH")


def _iverilog(modules: dict[str, Any], work: Path) -> list[list[str]]:
    """Every module in ``modules`` (each a copy of the one-cell netlist) driven with
    every row's steps at once, in iverilog on the sky130 models: per sample, each
    module's out and q."""
    work.mkdir(parents=True, exist_ok=True)
    netlist = work / "cells.json"
    netlist.write_text(json.dumps({"modules": modules}), encoding="utf-8")
    verilog = work / "cells.v"
    subprocess.run(
        ["yosys", "-q", "-p", f"read_json {netlist}; write_verilog -noattr {verilog}"],
        check=True,
    )
    lines = ["`timescale 1ns/1ps", "module tb;"]
    lines.append("  reg " + ", ".join(INPUTS) + ";")
    for k, name in enumerate(modules):
        lines.append(f"  wire out{k}, q{k};")
        pins = ", ".join(f".{p}({p})" for p in INPUTS)
        lines.append(f"  {name} m{k} ({pins}, .out(out{k}), .q(q{k}));")
    shown = ", ".join(f"out{k}, q{k}" for k in range(len(modules)))
    lines.append("  initial begin")
    for row in ROWS:
        for number, step in enumerate(_steps(row)):
            lines.append(
                "    " + " ".join(f"{p} = {int(step[p])};" for p in INPUTS) + " #5;"
            )
            if number in SAMPLED:
                lines.append(f'    $display("{"%b" * 2 * len(modules)}", {shown});')
            lines.append("    #5;")
    lines += ["    $finish;", "  end", "endmodule"]
    bench = work / "tb.v"
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
    compiled = work / "tb.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-DFUNCTIONAL", "-o", str(compiled)]
        + [str(bench), str(verilog), str(MODELS)],
        check=True,
        capture_output=True,
    )
    run = subprocess.run(
        ["vvp", "-n", str(compiled)], check=True, capture_output=True, text=True
    )
    samples = [line.strip() for line in run.stdout.splitlines() if line.strip()]
    samples = [line for line in samples if set(line) <= set("01xz")]
    return [[line[2 * k : 2 * k + 2] for k in range(len(modules))] for line in samples]


@pytest.mark.parametrize("side", SIDES)
def test_the_cell_does_what_its_spec_says(
    side: str, tmp_path: Path, tools: None, require_cpp_core: None
) -> None:
    """Every row: the cell's functional output and its flop before the edge, and its
    flop after it, are the spec's -- in FaultFlow's simulator and on the cells."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    netlist = tmp_path / "cell.json"
    netlist.write_text(json.dumps(_one_cell(side)), encoding="utf-8")
    before = core.fault_free_sequence_outputs(
        str(netlist), str(CELL_MAP), [_steps(row)[:3] for row in ROWS], INPUTS, OUTPUTS
    )
    after = core.fault_free_sequence_outputs(
        str(netlist), str(CELL_MAP), [_steps(row) for row in ROWS], INPUTS, ["q"]
    )
    cells = _iverilog(_one_cell(side)["modules"], tmp_path / "iverilog")
    assert len(cells) == 2 * len(ROWS)
    for k, row in enumerate(ROWS):
        out, next_q = spec(side, *row)
        q = row[3]
        assert (before[k]["out"], before[k]["q"], after[k]["q"]) == (
            bool(out),
            bool(q),
            bool(next_q),
        ), row
        assert cells[2 * k] == [f"{out}{q}"], row
        assert cells[2 * k + 1][0][1] == str(next_q), row


def _faulty(module: dict[str, Any], site: str, value: int) -> dict[str, Any]:
    """``module`` with stuck-at ``value`` at fault site ``site``: every reader of the
    net (a stem) or the one pin (a branch) reads the constant."""
    faulty = copy.deepcopy(module)
    parts = site.split(":")
    net = int(parts[1])
    constant = str(value)
    pins: list[tuple[str, str]] = []
    if parts[2] == "branch":
        pins.append((parts[3], parts[4]))
    else:
        for instance, cell in faulty["cells"].items():
            for pin, direction in cell["port_directions"].items():
                if direction == "input" and cell["connections"][pin] == [net]:
                    pins.append((instance, pin))
        for port in faulty["ports"].values():
            if port["direction"] == "output":
                port["bits"] = [constant if bit == net else bit for bit in port["bits"]]
    for instance, pin in pins:
        faulty["cells"][instance]["connections"][pin] = [constant]
    return faulty


@pytest.mark.parametrize("side", SIDES)
def test_faultflow_detects_a_fault_in_the_cell_exactly_when_the_cells_do(
    side: str, tmp_path: Path, tools: None, require_cpp_core: None
) -> None:
    """Every stuck-at FaultFlow enumerates on the cell (the clock's aside: they are
    excluded by default), over every row's steps: detected by FaultFlow's bit-parallel
    and reference simulators exactly when a copy of the cell with that fault differs
    from the good cell on the PDK's models."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    netlist = tmp_path / "cell.json"
    chip = _one_cell(side)
    netlist.write_text(json.dumps(chip), encoding="utf-8")
    clock = NETS["clk"]
    sites = [
        (str(row["site_key"]), int(row["compiled_net_index"]))
        for row in core.list_site_keys(str(netlist), str(CELL_MAP), "fail", [])
        if not str(row["site_key"]).startswith(f"net:{clock}:")
    ]
    faults = [(site, index, value) for site, index in sites for value in (0, 1)]
    cycles: list[list[bool]] = []
    sample: list[bool] = []
    for row in ROWS:
        for number, step in enumerate(_steps(row)):
            cycles.append([step[name] for name in INPUTS])
            sample.append(number in SAMPLED)
    specs = [(index, value) for _, index, value in faults]
    found = {}
    for reference in (False, True):
        result = core.simulate_sequence_faults(
            str(netlist),
            str(CELL_MAP),
            INPUTS,
            cycles,
            sample,
            OUTPUTS,
            specs,
            "fail",
            [],
            1,
            reference,
        )
        found[reference] = {
            (site, value)
            for (site, _, value), first in zip(faults, result["first_sample"])
            if int(first) >= 0
        }
    assert found[False] == found[True]

    good = chip["modules"]["wcell"]
    modules = {"wcell": good}
    for k, (site, _, value) in enumerate(faults):
        modules[f"wcell_f{k}"] = _faulty(good, site, value)
    samples = _iverilog(modules, tmp_path / "iverilog")
    on_cells = {
        (site, value)
        for k, (site, _, value) in enumerate(faults)
        if any(shown[k + 1] != shown[0] for shown in samples)
    }
    assert found[False] == on_cells
    assert on_cells, "the rows detect some of the cell's faults"
