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
