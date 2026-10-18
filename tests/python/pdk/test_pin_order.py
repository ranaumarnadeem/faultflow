"""A gate's inputs are wired by its cell-map entry's ``inputs`` order (in0, in1, ...),
not by pin names the compiler knows from Sky130: a library whose pins are named
otherwise (or a family whose name the compiler never saw) evaluates as its entry says.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _setup(tmp_path: Path, inputs: list[str]) -> tuple[Path, Path]:
    internal = json.loads(
        (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
    )
    cell_map = {k: v for k, v in internal.items() if k.lstrip("\\").startswith("$")}
    # AND2B is in0 & ~in1.
    cell_map["acme__andnot_*"] = {
        "node_type": "GATE",
        "gate_type": "AND2B",
        "inputs": inputs,
        "outputs": {"Z": "Z"},
    }
    map_path = tmp_path / "acme.json"
    map_path.write_text(json.dumps(cell_map), encoding="utf-8")
    module = {
        "attributes": {"top": "1"},
        "ports": {
            "P": {"direction": "input", "bits": [2]},
            "Q": {"direction": "input", "bits": [3]},
            "Z": {"direction": "output", "bits": [4]},
        },
        "cells": {
            "u": {
                "hide_name": 0,
                "type": "acme__andnot_1",
                "parameters": {},
                "attributes": {},
                "port_directions": {},
                "connections": {"P": [2], "Q": [3], "Z": [4]},
            }
        },
        "netnames": {},
    }
    netlist = tmp_path / "top.json"
    netlist.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")
    return netlist, map_path


@pytest.mark.unit
@pytest.mark.parametrize(
    ("inputs", "fn"),
    [(["P", "Q"], lambda p, q: p and not q), (["Q", "P"], lambda p, q: q and not p)],
)
def test_the_entrys_input_order_wires_the_gate(
    tmp_path: Path, require_cpp_core: None, inputs: list[str], fn: Any
) -> None:
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    netlist, map_path = _setup(tmp_path, inputs)
    vectors = [{"P": p, "Q": q} for p in (False, True) for q in (False, True)]
    got = core.fault_free_outputs(
        str(netlist), str(map_path), vectors, ["P", "Q"], ["Z"], "fail"
    )
    assert [bool(r["Z"]) for r in got] == [fn(v["P"], v["Q"]) for v in vectors]


@pytest.mark.unit
def test_a_two_output_tie_cell_drives_both_constants(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Sky130's conb: HI is 1, LO is 0 -- each a constant net of its own."""
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    internal = json.loads(
        (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
    )
    cell_map = {k: v for k, v in internal.items() if k.lstrip("\\").startswith("$")}
    cell_map["acme__conb_*"] = {
        "node_type": "CONST",
        "inputs": [],
        "outputs": {"HI": "HI", "LO": "LO"},
        "constants": {"HI": 1, "LO": 0},
    }
    cell_map["acme__xor2_*"] = {
        "node_type": "GATE",
        "gate_type": "XOR2",
        "inputs": ["A", "B"],
        "outputs": {"X": "X"},
    }
    map_path = tmp_path / "acme.json"
    map_path.write_text(json.dumps(cell_map), encoding="utf-8")
    cell = {"hide_name": 0, "parameters": {}, "attributes": {}, "port_directions": {}}
    module = {
        "attributes": {"top": "1"},
        "ports": {
            "P": {"direction": "input", "bits": [2]},
            "H": {"direction": "output", "bits": [3]},
            "L": {"direction": "output", "bits": [4]},
            "Z": {"direction": "output", "bits": [5]},
        },
        "cells": {
            "t": {
                **cell,
                "type": "acme__conb_1",
                "connections": {"HI": [3], "LO": [4]},
            },
            "x": {
                **cell,
                "type": "acme__xor2_1",
                "connections": {"A": [2], "B": [3], "X": [5]},
            },
        },
        "netnames": {},
    }
    netlist = tmp_path / "top.json"
    netlist.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")
    got = core.fault_free_outputs(
        str(netlist),
        str(map_path),
        [{"P": False}, {"P": True}],
        ["P"],
        ["H", "L", "Z"],
        "fail",
    )
    assert [dict(r) for r in got] == [
        {"H": True, "L": False, "Z": True},
        {"H": True, "L": False, "Z": False},
    ]
