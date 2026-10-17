"""The cells FaultFlow instantiates itself -- scan flops for stitching, the IEEE 1500
wrapper's boundary cells and WSP, the gates an enable flop's hold folds into -- by
role, from whatever library the design's cell map is: a role is a logical function
with logical pins (a non-inverting mux2: a0, a1, s -> out; a flop with an active-low
reset: clk, d, reset_n -> q), and the library's cell for it is found and checked
against that function with the core's own gate evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.pdk.command import cell_map_command
from faultflow.pdk.roles import PdkError, library_roles

from .test_flop_liberty import LIBRARIES

ROOT = Path(__file__).resolve().parents[3]
HD_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _map(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.unit
def test_sky130_hd_roles_are_the_cells_faultflow_has_always_used(
    require_cpp_core: None,
) -> None:
    roles = library_roles(_map(HD_MAP))
    expect = {
        "scan_ff": (
            "sky130_fd_sc_hd__sdfxtp_1",
            {"clk": "CLK", "d": "D", "si": "SCD", "se": "SCE", "q": "Q"},
        ),
        "scan_ff_reset": (
            "sky130_fd_sc_hd__sdfrtp_1",
            {
                "clk": "CLK",
                "d": "D",
                "si": "SCD",
                "se": "SCE",
                "reset_n": "RESET_B",
                "q": "Q",
            },
        ),
        "scan_ff_set": (
            "sky130_fd_sc_hd__sdfstp_1",
            {
                "clk": "CLK",
                "d": "D",
                "si": "SCD",
                "se": "SCE",
                "set_n": "SET_B",
                "q": "Q",
            },
        ),
        "ff": ("sky130_fd_sc_hd__dfxtp_1", {"clk": "CLK", "d": "D", "q": "Q"}),
        "ff_reset": (
            "sky130_fd_sc_hd__dfrtp_1",
            {"clk": "CLK", "d": "D", "reset_n": "RESET_B", "q": "Q"},
        ),
        "ff_reset_negedge": (
            "sky130_fd_sc_hd__dfrtn_1",
            {"clk": "CLK_N", "d": "D", "reset_n": "RESET_B", "q": "Q"},
        ),
        "and2": ("sky130_fd_sc_hd__and2_1", {"a": "A", "b": "B", "out": "X"}),
        "or2": ("sky130_fd_sc_hd__or2_1", {"a": "A", "b": "B", "out": "X"}),
        "xor2": ("sky130_fd_sc_hd__xor2_1", {"a": "A", "b": "B", "out": "X"}),
        "inv": ("sky130_fd_sc_hd__inv_1", {"a": "A", "out": "Y"}),
        "and2b": ("sky130_fd_sc_hd__and2b_1", {"a_n": "A_N", "b": "B", "out": "X"}),
        "mux2": (
            "sky130_fd_sc_hd__mux2_1",
            {"a0": "A0", "a1": "A1", "s": "S", "out": "X"},
        ),
    }
    for role, (cell, pins) in expect.items():
        got = roles[role]
        assert (got.cell, dict(got.pins)) == (cell, pins), role


@pytest.mark.integration
@pytest.mark.parametrize("lib,liberty", LIBRARIES, ids=[lib for lib, _ in LIBRARIES])
def test_every_sky130_library_has_its_own_cell_for_every_role(
    lib: str, liberty: Path, tmp_path: Path, require_cpp_core: None
) -> None:
    """hvl has no scan flop with an active-low set: the role is refused, by name."""
    out = tmp_path / f"{lib}.json"
    cell_map_command(liberty, out)
    roles = library_roles(_map(out))
    for role in roles.names():
        try:
            got = roles[role]
        except PdkError as exc:
            assert role in str(exc) and lib in str(exc)
            continue
        assert got.cell.startswith(lib + "__"), (role, got.cell)


@pytest.mark.unit
def test_a_role_the_library_cannot_fill_is_refused_by_name(
    require_cpp_core: None,
) -> None:
    cm = {
        "acme__nand2_*": {
            "node_type": "GATE",
            "gate_type": "NAND2",
            "inputs": ["A", "B"],
            "outputs": {"Y": "Y"},
        }
    }
    roles = library_roles(cm, name="acme")
    with pytest.raises(PdkError, match="acme.*mux2"):
        roles["mux2"]


@pytest.mark.unit
def test_roles_follow_the_libraries_own_pin_names(require_cpp_core: None) -> None:
    """A mux whose pins are named otherwise, with its inputs in the gate's order."""
    cm = {
        "acme__mx2_*": {
            "node_type": "GATE",
            "gate_type": "MUX2_NI",
            "inputs": ["I0", "I1", "SEL"],
            "outputs": {"Z": "Z"},
            "variants": ["acme__mx2_x2", "acme__mx2_x1"],
        }
    }
    got = library_roles(cm, name="acme")["mux2"]
    assert got.cell == "acme__mx2_x1"
    assert dict(got.pins) == {"a0": "I0", "a1": "I1", "s": "SEL", "out": "Z"}


@pytest.mark.unit
def test_an_hd_role_cell_is_the_cell_faultflow_always_built(
    require_cpp_core: None,
) -> None:
    from faultflow.pdk.roles import default_roles, role_cell

    cell = role_cell(
        default_roles(), "mux2", {"role": "mux"}, {"a0": 3, "a1": 4, "s": 5}, {"out": 6}
    )
    assert cell == {
        "hide_name": 0,
        "type": "sky130_fd_sc_hd__mux2_1",
        "parameters": {},
        "attributes": {"role": "mux"},
        "port_directions": {"A0": "input", "A1": "input", "S": "input", "X": "output"},
        "connections": {"A0": [3], "A1": [4], "S": [5], "X": [6]},
    }


@pytest.mark.unit
def test_a_role_cell_on_other_pins_says_which_are_which(require_cpp_core: None) -> None:
    from faultflow.pdk.roles import PINS_ATTR, cell_pin, role_cell

    cm = {
        "acme__mx2_*": {
            "node_type": "GATE",
            "gate_type": "MUX2_NI",
            "inputs": ["I0", "I1", "SEL"],
            "outputs": {"Z": "Z"},
            "variants": ["acme__mx2_x1"],
        }
    }
    roles = library_roles(cm, name="acme")
    cell = role_cell(roles, "mux2", {}, {"a0": 3, "a1": 4, "s": 5}, {"out": 6})
    assert cell["type"] == "acme__mx2_x1"
    assert cell["connections"] == {"I0": [3], "I1": [4], "SEL": [5], "Z": [6]}
    assert cell["attributes"][PINS_ATTR] == "a0=I0 a1=I1 s=SEL out=Z"
    assert cell_pin(cell, "a0", "A0") == "I0"
    assert cell_pin(cell, "s", "S") == "SEL"
    # A cell without the attribute: the Sky130 HD name.
    assert cell_pin({"attributes": {}}, "a0", "A0") == "A0"


@pytest.mark.unit
def test_hd_pins_are_hds_roles(require_cpp_core: None) -> None:
    from faultflow.pdk.roles import HD_PINS, default_roles

    roles = default_roles()
    assert {role: dict(roles[role].pins) for role in HD_PINS} == HD_PINS
