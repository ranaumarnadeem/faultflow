"""Every combinational cell of both cell maps, simulated through the C++ core on a
one-cell netlist in every input combination, against the cell's own Liberty function.

The compiler wires a cell's pins to its gate's inputs by pin name, from a table of its
own: a pin the table doesn't name is silently left unconnected, and one it places
wrong swaps the gate's inputs. Checking each cell's truth table end to end, against
the vendor's function rather than gate_eval.cpp's formula, catches both. (The
truth-table tests of tests/cpp check gate_eval.cpp's formulas, in the cell map's
order -- not the wiring.)
"""

from __future__ import annotations

import functools
import importlib.util
import itertools
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[2]
SKY130 = (ROOT / "cells/sky130/sky130_fd_sc_hd.json", "sky130_fd_sc_hd__tt_025C_1v80")
OSU = (ROOT / "cells/osu/osu035.json", "osu035_stdcells")
# A cell Liberty doesn't have: (the Liberty cell with its function, Liberty pin ->
# the cell's pin). OpenTestability's control-point muxes are MUX2X1 with select S0.
ALIASES = {name: ("MUX2X1", {"S": "S0"}) for name in ("MX2X1", "MX2X2", "MX2X4")}


@functools.lru_cache(maxsize=None)
def _tool() -> ModuleType:
    path = ROOT / "tools/derive_gate_truth_tables.py"
    spec = importlib.util.spec_from_file_location("derive_gate_truth_tables", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _liberty(cell_map: Path) -> str:
    lib = cell_map.parent / f"{dict((SKY130, OSU))[cell_map]}.lib"
    return lib.read_text(encoding="utf-8", errors="replace")


def _gates() -> Iterator[Any]:
    for cell_map, _lib in (SKY130, OSU):
        entries = json.loads(cell_map.read_text(encoding="utf-8"))
        for pattern, entry in entries.items():
            if (
                isinstance(entry, dict)
                and entry.get("node_type") == "GATE"
                and not entry.get("unsupported")
                and not pattern.startswith(("$", "\\"))
            ):
                yield pytest.param(cell_map, pattern, entry, id=pattern)


def _functions(
    cell_map: Path, pattern: str, entry: dict[str, Any]
) -> tuple[str, dict[str, str], dict[str, str]]:
    """(the cell to instantiate, its Liberty function per output pin, Liberty pin
    -> the cell's pin)."""
    tool = _tool()
    outputs = list(entry["outputs"].values())
    target, renames = ALIASES.get(pattern, (None, {}))
    for name in [target] if target else tool.concrete_liberty_names(pattern):
        found = tool.extract_liberty_functions(_liberty(cell_map), [name], outputs)
        if found:
            cell = pattern if target else name
            return cell, found, renames
    raise AssertionError(f"{pattern}: no Liberty function for {outputs}")


@pytest.mark.golden
@pytest.mark.parametrize(("cell_map", "pattern", "entry"), list(_gates()))
def test_each_cell_computes_its_liberty_function(
    cell_map: Path,
    pattern: str,
    entry: dict[str, Any],
    tmp_path: Path,
    require_cpp_core: None,
) -> None:
    cell, functions, renames = _functions(cell_map, pattern, entry)
    inputs = list(entry["inputs"])
    outputs = list(entry["outputs"].values())
    ports: dict[str, Any] = {
        f"i_{pin}": {"direction": "input", "bits": [2 + i]}
        for i, pin in enumerate(inputs)
    }
    connections = {pin: [2 + i] for i, pin in enumerate(inputs)}
    for k, pin in enumerate(outputs):
        ports[f"o_{pin}"] = {"direction": "output", "bits": [100 + k]}
        connections[pin] = [100 + k]
    netlist = tmp_path / "cell.json"
    module = {
        "attributes": {"top": "00000000000000000000000000000001"},
        "ports": ports,
        "cells": {
            "u": {
                "type": cell,
                "port_directions": {
                    pin: "output" if pin in outputs else "input" for pin in connections
                },
                "connections": connections,
            }
        },
        "netnames": {name: {"bits": port["bits"]} for name, port in ports.items()},
    }
    netlist.write_text(json.dumps({"modules": {"cell": module}}), encoding="utf-8")
    names = [f"i_{pin}" for pin in inputs]
    vectors = [
        dict(zip(names, bits))
        for bits in itertools.product((False, True), repeat=len(inputs))
    ]
    core = _load_core()
    assert core is not None
    results = core.fault_free_outputs(
        str(netlist), str(cell_map), vectors, names, [f"o_{p}" for p in outputs]
    )
    index = {pin: i for i, pin in enumerate(inputs)}
    liberty_index = {lib: index[renames.get(lib, lib)] for lib in [*renames, *index]}
    osu = cell_map == OSU[0]
    for pin in outputs:
        for vector, result in zip(vectors, results):
            bits = [vector[name] for name in names]
            want = _tool().eval_liberty_expr(functions[pin], liberty_index, osu, bits)
            assert bool(result[f"o_{pin}"]) == bool(want), (
                f"{pattern} {pin} = {functions[pin]!r} with "
                f"{dict(zip(inputs, map(int, bits)))}: the simulator gives "
                f"{int(result[f'o_{pin}'])}"
            )
