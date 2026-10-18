"""Every flop a generated cell map models, simulated through the C++ core on a one-cell
netlist, against the cell's own Liberty ``ff`` group evaluated here: ``next_state`` on
the clock edge, ``clear``/``preset`` as a level-sensitive force (both: the library's
``clear_preset_var1``), Q and Q_N. The vendor's model, not the cell map's, is the
reference -- as tests/python/test_cell_liberty.py does for the combinational cells."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from faultflow.pdk.boolexpr import parse_expr
from faultflow.pdk.command import cell_map_command
from faultflow.pdk.liberty import Group, parse_liberty

from .installed import installed, pdk_root_with

ROOT = Path(__file__).resolve().parents[3]
HD_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
HD_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
STEPS = 10


def _installed_lib(pdk: str, lib: str) -> Path | None:
    """The library's Liberty at its nominal corner (faultflow.pdk.resolve's choice;
    cell functions don't change with the corner), if its PDK is installed."""
    from faultflow.pdk.resolve import _corner

    root = pdk_root_with(pdk, lib)
    if root is None:
        return None
    return _corner(root / pdk / "libs.ref" / lib / "lib", lib, None)


# The shipped HD Liberty, then every other library of every installed PDK.
LIBRARIES = [("sky130_fd_sc_hd", HD_LIB)] + [
    (lib, path)
    for pdk, lib in installed()
    if lib != "sky130_fd_sc_hd" and (path := _installed_lib(pdk, lib)) is not None
]


def _families(library: Group, cell_map: dict[str, Any]) -> list[Group]:
    """One cell (the smallest drive strength) per modelled flop family."""
    import fnmatch

    seen: set[str] = set()
    out = []
    for cell in library.all("cell"):
        name = str(cell.args[0])
        key = next((k for k in cell_map if fnmatch.fnmatchcase(name, k)), None)
        if key is None or key in seen or cell_map[key].get("node_type") != "FF":
            continue
        seen.add(key)
        out.append(cell)
    return out


def _matches(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)


def _pins(cell: Group) -> dict[str, Group]:
    return {str(p.args[0]): p for p in cell.all("pin")}


def _model(cell: Group, steps: list[dict[str, bool]]) -> list[dict[str, bool]]:
    """Q and Q_N after each step, from the cell's Liberty ff group."""
    ff = cell.find("ff")
    assert ff is not None
    iq, iqn = str(ff.args[0]), str(ff.args[1])
    nxt = parse_expr(ff.attrs["next_state"])
    clear = parse_expr(ff.attrs["clear"]) if "clear" in ff.attrs else None
    preset = parse_expr(ff.attrs["preset"]) if "preset" in ff.attrs else None
    both = ff.attrs.get("clear_preset_var1", "L") == "H"
    outputs = {
        p: g.attrs["function"].strip()
        for p, g in _pins(cell).items()
        if g.attrs.get("direction") == "output"
    }
    state = False
    out = []
    for env in steps:
        # Each step is one clock pulse (the inactive level, then the active one) with
        # the other inputs held: the controls force, else the edge loads next_state.
        c = clear is not None and clear.eval(env)
        p = preset is not None and preset.eval(env)
        if c and p:
            state = both
        elif c:
            state = False
        elif p:
            state = True
        else:
            state = nxt.eval({**env, iq: state, iqn: not state})
        out.append(
            {pin: state if fn == iq else not state for pin, fn in outputs.items()}
        )
    return out


def _clock_pin(cell: Group) -> tuple[str, bool]:
    ff = cell.find("ff")
    assert ff is not None
    expr = parse_expr(ff.attrs["clocked_on"])
    if expr.op == "var":
        return expr.name, True
    return expr.args[0].name, False


@pytest.mark.integration
@pytest.mark.parametrize("lib,liberty", LIBRARIES, ids=[lib for lib, _ in LIBRARIES])
def test_every_modelled_flop_is_its_liberty_model(
    lib: str, liberty: Path, tmp_path: Path, require_cpp_core: None
) -> None:
    map_path = tmp_path / f"{lib}.json"
    cell_map_command(liberty, map_path)
    _check_flops(lib, liberty, map_path, tmp_path)


@pytest.mark.integration
def test_every_flop_the_shipped_hd_map_models_is_its_liberty_model(
    tmp_path: Path, require_cpp_core: None
) -> None:
    _check_flops("sky130_fd_sc_hd", HD_LIB, HD_MAP, tmp_path)


def _check_flops(lib: str, liberty: Path, map_path: Path, tmp_path: Path) -> None:
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    cell_map = json.loads(map_path.read_text(encoding="utf-8"))
    library = parse_liberty(liberty.read_text(encoding="utf-8", errors="replace"))
    families = _families(library, cell_map)
    assert families, f"{lib}: no flop families"
    rng = random.Random(lib)
    failures = []
    for cell in families:
        name = str(cell.args[0])
        pins = _pins(cell)
        inputs = [p for p, g in pins.items() if g.attrs.get("direction") == "input"]
        outputs = [p for p, g in pins.items() if g.attrs.get("direction") == "output"]
        clock, rising = _clock_pin(cell)
        data_inputs = [p for p in inputs if p != clock]
        bits = {p: 10 + i for i, p in enumerate(inputs + outputs)}
        module = {
            "attributes": {"top": "1"},
            "ports": {
                p: {
                    "direction": "input" if p in inputs else "output",
                    "bits": [bits[p]],
                }
                for p in inputs + outputs
            },
            "cells": {
                "u": {
                    "hide_name": 0,
                    "type": name,
                    "parameters": {},
                    "attributes": {},
                    "port_directions": {},
                    "connections": {p: [bits[p]] for p in inputs + outputs},
                }
            },
            "netnames": {},
        }
        netlist = tmp_path / f"{name}.json"
        netlist.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")
        # Async controls mostly inactive, so the clocked path is exercised.
        steps = []
        for _ in range(STEPS):
            env = {p: rng.random() < 0.5 for p in data_inputs}
            steps.append(env)
        ff = cell.find("ff")
        assert ff is not None
        scan = cell_map[next(k for k in cell_map if _matches(name, k))]["ff"].get(
            "scan"
        )
        for key in ("clear", "preset"):
            if key in ff.attrs:
                ctl = parse_expr(ff.attrs[key])
                pin = sorted(ctl.variables())[0]
                for env in steps:
                    # The engine ignores a scan flop's async controls while scan
                    # enable is on: scan-check refuses a design that doesn't hold
                    # them inactive in shift. Keep to that contract here.
                    shifting = scan is not None and env[scan["enable"]] == (
                        scan["enable_polarity"] == "HIGH"
                    )
                    if shifting or rng.random() < 0.75:
                        for value in (False, True):
                            if not ctl.eval({**env, pin: value}):
                                env[pin] = value
                                break
        expected = _model(cell, steps)

        def pulse(env: dict[str, bool]) -> list[dict[str, bool]]:
            return [{**env, clock: not rising}, {**env, clock: rising}]

        sequences = [
            [cycle for env in steps[: k + 1] for cycle in pulse(env)]
            for k in range(STEPS)
        ]
        got = core.fault_free_sequence_outputs(
            str(netlist), str(map_path), sequences, inputs, outputs, "fail"
        )
        for k in range(STEPS):
            if dict(got[k]) != expected[k]:
                failures.append((name, k, steps[k], dict(got[k]), expected[k]))
                break
    assert failures == [], "\n".join(
        f"{name} step {k}: inputs {env} got {got} expected {want}"
        for name, k, env, got, want in failures
    )
