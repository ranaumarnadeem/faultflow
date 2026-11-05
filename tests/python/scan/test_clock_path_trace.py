"""faultflow.scan.clock_gates.clock_path on a hand-built netlist: a flop clock pin's
net followed back through clock buffers and inverters, clock gates, and the cells
held inputs keep passing the clock on, to the net none of them drives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.control_trace import Netlist
from faultflow.scan.clock_gates import clock_gates, clock_path

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads(
    (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
)
HD = "sky130_fd_sc_hd__"


def _cell(kind: str, conns: dict[str, Any], output: str) -> dict[str, Any]:
    return {
        "type": HD + kind,
        "port_directions": {p: "output" if p == output else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


# clk (2) -> u_b1 -> 3; 3 -> u_i -> 4 (inverted); mux2 (A0=3, A1=pll 5, S=tm 6) -> 7;
# and2 (3, en 8) -> 9; dlclkp (CLK=3, GATE=g 10) -> 11 -> u_b2 -> 12;
# a loop of two buffers (20 <-> 21).
MODULE: dict[str, Any] = {
    "ports": {
        name: {"direction": "input", "bits": [net]}
        for name, net in (("clk", 2), ("pll", 5), ("tm", 6), ("en", 8), ("g", 10))
    },
    "cells": {
        "u_b1": _cell("clkbuf_1", {"A": 2, "X": 3}, "X"),
        "u_i": _cell("clkinv_1", {"A": 3, "Y": 4}, "Y"),
        "u_m": _cell("mux2_1", {"A0": 3, "A1": 5, "S": 6, "X": 7}, "X"),
        "u_a": _cell("and2_1", {"A": 3, "B": 8, "X": 9}, "X"),
        "u_cg": _cell("dlclkp_1", {"CLK": 3, "GATE": 10, "GCLK": 11}, "GCLK"),
        "u_b2": _cell("clkbuf_1", {"A": 11, "X": 12}, "X"),
        "u_l1": _cell("clkbuf_1", {"A": 20, "X": 21}, "X"),
        "u_l2": _cell("clkbuf_1", {"A": 21, "X": 20}, "X"),
    },
}


@pytest.fixture
def traced(require_cpp_core: None) -> tuple[Netlist, Any]:
    return Netlist(MODULE, CELL_MAP), clock_gates(MODULE, CELL_MAP)


def test_a_buffered_clock_reaches_its_port(traced: tuple[Netlist, Any]) -> None:
    netlist, gates = traced
    found = clock_path(3, gates, netlist)
    assert (found.root, found.inverted, found.nets, found.gates) == (
        2,
        False,
        (3, 2),
        (),
    )


def test_an_inverter_on_the_way_inverts_the_clock(traced: tuple[Netlist, Any]) -> None:
    netlist, gates = traced
    found = clock_path(4, gates, netlist)
    assert (found.root, found.inverted, found.nets) == (2, True, (4, 3, 2))


def test_a_clock_mux_passes_the_clock_its_held_select_picks(
    traced: tuple[Netlist, Any],
) -> None:
    netlist, gates = traced
    assert clock_path(7, gates, netlist).root == 7  # nothing holds the select
    held = clock_path(7, gates, netlist, {"tm": 0})
    assert (held.root, held.nets) == (2, (7, 3, 2))
    assert [(net, cell, pin, value) for net, cell, pin, value in held.holds] == [
        (6, "u_m", "S", 0)
    ]
    assert clock_path(7, gates, netlist, {"tm": 1}).root == 5  # the other clock


def test_an_and_passes_the_clock_while_its_other_input_is_held_at_one(
    traced: tuple[Netlist, Any],
) -> None:
    netlist, gates = traced
    assert clock_path(9, gates, netlist).root == 9
    assert clock_path(9, gates, netlist, {"en": 1}).root == 2
    assert clock_path(9, gates, netlist, {"en": 0}).root == 9  # held off: no clock


def test_buffers_on_both_sides_of_a_clock_gate(traced: tuple[Netlist, Any]) -> None:
    netlist, gates = traced
    found = clock_path(12, gates, netlist)
    assert found.root == 2 and found.nets == (12, 11, 3, 2)
    assert [g.instance for g in found.gates] == ["u_cg"]
    # Without a netlist only the clock gates are followed.
    assert clock_path(12, gates).root == 12
    unbuffered = clock_path(11, gates)
    assert (unbuffered.root, unbuffered.gates) == (3, found.gates)


def test_a_loop_of_buffers_ends_the_trace(traced: tuple[Netlist, Any]) -> None:
    netlist, gates = traced
    found = clock_path(21, gates, netlist)
    assert found.root in (20, 21) and set(found.nets) <= {20, 21}
