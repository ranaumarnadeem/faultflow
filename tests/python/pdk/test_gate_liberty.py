"""Every combinational cell a generated cell map models, in every Sky130 library on this
machine, simulated through the C++ core -- the compiler's own pin wiring -- in every
input combination, against the cell's own Liberty ``function``. The generator matched
each function with the core's gate evaluation; this checks the whole path a netlist
takes (normalization, pin order, multi-output lowering) against the vendor's model, as
tests/python/test_cell_liberty.py does for the shipped maps."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.pdk.boolexpr import parse_expr
from faultflow.pdk.command import cell_map_command
from faultflow.pdk.liberty import parse_liberty

from .test_flop_liberty import LIBRARIES


@pytest.mark.integration
@pytest.mark.parametrize("lib,liberty", LIBRARIES, ids=[lib for lib, _ in LIBRARIES])
def test_every_modelled_gate_is_its_liberty_function(
    lib: str, liberty: Path, tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    map_path = tmp_path / f"{lib}.json"
    cell_map_command(liberty, map_path)
    cell_map = json.loads(map_path.read_text(encoding="utf-8"))
    library = parse_liberty(liberty.read_text(encoding="utf-8", errors="replace"))

    cells: dict[str, Any] = {}
    ports: dict[str, Any] = {}
    checks: list[tuple[str, str, list[str], str]] = []  # cell, out port, ins, function
    seen: set[str] = set()
    net = 10
    for cell in library.all("cell"):
        name = str(cell.args[0])
        key = next((k for k in cell_map if fnmatch.fnmatchcase(name, k)), None)
        if key is None or key in seen or cell_map[key].get("node_type") != "GATE":
            continue
        seen.add(key)
        pins = {str(p.args[0]): p for p in cell.all("pin")}
        inputs = [p for p, g in pins.items() if g.attrs.get("direction") == "input"]
        outputs = [p for p, g in pins.items() if g.attrs.get("direction") == "output"]
        inst = f"u{len(cells)}"
        conns: dict[str, list[int]] = {}
        port_of: dict[str, str] = {}
        for pin in inputs + outputs:
            port = f"{inst}_{pin}"
            conns[pin] = [net]
            ports[port] = {
                "direction": "input" if pin in inputs else "output",
                "bits": [net],
            }
            port_of[pin] = port
            net += 1
        cells[inst] = {
            "hide_name": 0,
            "type": name,
            "parameters": {},
            "attributes": {},
            "port_directions": {},
            "connections": conns,
        }
        for out in outputs:
            checks.append(
                (
                    name,
                    port_of[out],
                    [port_of[p] for p in inputs],
                    pins[out].attrs["function"],
                )
            )
    assert checks, f"{lib}: no gates"
    module = {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": {},
    }
    netlist = tmp_path / "gates.json"
    netlist.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")

    in_ports = [p for p, d in ports.items() if d["direction"] == "input"]
    out_ports = [p for p, d in ports.items() if d["direction"] == "output"]
    # Vector i drives each cell's k-th input with bit k of i: 64 vectors cover every
    # combination of every cell of up to six inputs.
    index_of = {}
    for name, out, ins, fn in checks:
        for k, port in enumerate(ins):
            index_of[port] = k
    vectors = [
        {port: bool((i >> index_of.get(port, 0)) & 1) for port in in_ports}
        for i in range(64)
    ]
    got = core.fault_free_outputs(
        str(netlist), str(map_path), vectors, in_ports, out_ports, "fail"
    )
    failures = []
    for name, out, ins, fn in checks:
        expr = parse_expr(fn)
        pin_of = {port: port.split("_", 1)[1] for port in ins}
        for i in range(1 << len(ins)):
            env = {pin_of[p]: bool((i >> k) & 1) for k, p in enumerate(ins)}
            if bool(got[i][out]) != expr.eval(env):
                failures.append(f"{name} {out}: {env} gave {bool(got[i][out])}")
                break
    assert failures == [], "\n".join(failures)
