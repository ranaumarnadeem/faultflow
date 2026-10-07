"""sky130 dfrtn: a flop that takes D on the falling edge of CLK_N, cleared while
RESET_B is low -- the IEEE 1500 wrapper's instruction register updates on the falling
edge of WRCK, and WRSTN resets it. Its cell-map entry, and FaultFlow's simulation of
it against the PDK's own cell model (iverilog) over a random sequence."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from faultflow.cell_map import load_cell_map, lookup_cell

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
CELL = "sky130_fd_sc_hd__dfrtn_1"
INPUTS = ("clk_n", "d", "rst_n")


@pytest.mark.unit
def test_dfrtn_resolves_as_a_falling_edge_flop_with_a_clear() -> None:
    entry = lookup_cell(CELL, load_cell_map(CELL_MAP))
    assert entry is not None and entry["node_type"] == "FF"
    ff = entry["ff"]
    assert (ff["clock"], ff["data"], ff["output"], ff["trigger"]) == (
        "CLK_N",
        "D",
        "Q",
        "NEGEDGE",
    )
    assert ff["clear"] == {"pin": "RESET_B", "level": "LOW", "value": 0}
    assert "preset" not in ff


def _steps(seed: int, count: int) -> list[dict[str, int]]:
    """A cleared start, then each step changes the clock alone or the data and
    reset alone -- never both, which a cell model would race."""
    rng = random.Random(seed)
    state = {"clk_n": 1, "d": 0, "rst_n": 0}
    steps = [dict(state)]
    for _ in range(count):
        if rng.random() < 0.5:
            state["clk_n"] ^= 1
        else:
            state["d"] = rng.randint(0, 1)
            state["rst_n"] = int(rng.random() < 0.85)
        steps.append(dict(state))
    return steps


@pytest.mark.integration
def test_faultflow_simulates_dfrtn_as_the_cell_model_does(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    module = {
        "attributes": {"top": "1"},
        "ports": {
            "clk_n": {"direction": "input", "bits": [2]},
            "d": {"direction": "input", "bits": [3]},
            "rst_n": {"direction": "input", "bits": [4]},
            "q": {"direction": "output", "bits": [5]},
        },
        "cells": {
            "u": {
                "hide_name": 0,
                "type": CELL,
                "parameters": {},
                "attributes": {},
                "port_directions": {
                    "CLK_N": "input",
                    "D": "input",
                    "RESET_B": "input",
                    "Q": "output",
                },
                "connections": {"CLK_N": [2], "D": [3], "RESET_B": [4], "Q": [5]},
            }
        },
        "netnames": {},
    }
    netlist = tmp_path / "dfrtn.json"
    netlist.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")
    steps = _steps(1500, 300)
    result = core.simulate_sequence_faults(
        str(netlist),
        str(CELL_MAP),
        list(INPUTS),
        [[bool(step[name]) for name in INPUTS] for step in steps],
        [True] * len(steps),
        ["q"],
        [],
    )
    faultflow = [int(sample[0]) for sample in result["golden"]]

    lines = [
        "`timescale 1ns/1ps",
        "module tb;",
        "  reg clk_n = 1, d = 0, rst_n = 0;",
        "  wire q;",
        f"  {CELL} u(.CLK_N(clk_n), .D(d), .RESET_B(rst_n), .Q(q));",
        "  initial begin",
    ]
    for step in steps:
        lines += [f"    {name} = {step[name]};" for name in INPUTS]
        lines += ["    #5;", '    $display("Q %b", q);']
    lines += ["    $finish;", "  end", "endmodule"]
    bench = tmp_path / "tb.v"
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sim = tmp_path / "tb.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-DFUNCTIONAL", "-o", str(sim), str(bench), str(MODELS)],
        check=True,
        capture_output=True,
        text=True,
    )
    shown = subprocess.run(
        ["vvp", "-n", str(sim)], check=True, capture_output=True, text=True
    ).stdout.split()
    cells = [int(value) for tag, value in zip(shown[::2], shown[1::2]) if tag == "Q"]
    assert len(cells) == len(steps)
    assert faultflow == cells
    # The sequence exercised what matters: falling edges with D at 0 and at 1, and
    # the clear.
    assert 0 in faultflow and 1 in faultflow
