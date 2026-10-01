"""The shared tracer (faultflow.control_trace): the controlling-value table against the
simulator's own gate evaluation, and what Netlist.forced follows and records."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.control_trace import CONTROLLING, Netlist, release_faults
from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
INPUTS = ("a", "b", "c", "d")


def _sky130_cell(gate: str) -> tuple[str, list[str], str]:
    """(cell type, input pins in order, output pin) of a sky130 cell mapped to
    ``gate``."""
    for pattern, entry in CELL_MAP.items():
        if (
            isinstance(entry, dict)
            and entry.get("gate_type") == gate
            and pattern.startswith("sky130_fd_sc_hd__")
            and "lpflow" not in pattern
        ):
            return (
                pattern.replace("*", "1"),
                entry["inputs"],
                next(iter(entry["outputs"])),
            )
    raise AssertionError(f"no sky130 cell for {gate}")


@pytest.mark.unit
def test_each_controlling_value_sets_the_output_the_simulator_computes(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """One cell per gate type on inputs a..d: in every input combination, an input at
    its controlling value gives the output the table says, and with none there the
    other output -- as gate_eval.cpp computes them."""
    nets = {name: 2 + i for i, name in enumerate(INPUTS)}
    ports: dict[str, Any] = {
        name: {"direction": "input", "bits": [net]} for name, net in nets.items()
    }
    cells: dict[str, Any] = {}
    for index, gate in enumerate(sorted(CONTROLLING)):
        cell, pins, out = _sky130_cell(gate)
        net = 100 + index
        cells[f"g_{gate}"] = {
            "type": cell,
            "port_directions": {**{p: "input" for p in pins}, out: "output"},
            "connections": {
                **{p: [nets[INPUTS[i]]] for i, p in enumerate(pins)},
                out: [net],
            },
        }
        ports[f"y_{gate}"] = {"direction": "output", "bits": [net]}
    netlist = tmp_path / "gates.json"
    netlist.write_text(
        json.dumps(
            {
                "modules": {
                    "gates": {
                        "attributes": {"top": "00000000000000000000000000000001"},
                        "ports": ports,
                        "cells": cells,
                        "netnames": {n: {"bits": p["bits"]} for n, p in ports.items()},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    vectors = [
        dict(zip(INPUTS, values))
        for values in itertools.product((False, True), repeat=len(INPUTS))
    ]
    outputs = [f"y_{gate}" for gate in sorted(CONTROLLING)]
    core = _load_core()
    assert core is not None
    results = core.fault_free_outputs(
        str(netlist), str(CELL_MAP_PATH), vectors, list(INPUTS), outputs
    )
    for gate, (setting, output) in CONTROLLING.items():
        for vector, result in zip(vectors, results):
            values = [int(vector[INPUTS[i]]) for i in range(len(setting))]
            controlled = any(v == c for v, c in zip(values, setting))
            expected = output if controlled else 1 - output
            assert int(result[f"y_{gate}"]) == expected, (gate, values)


def _cell(cell_type: str, **conns: Any) -> dict[str, Any]:
    outputs = {"Q", "X", "Y"}
    return {
        "type": f"sky130_fd_sc_hd__{cell_type}",
        "port_directions": {p: ("output" if p in outputs else "input") for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _netlist(**cells: dict[str, Any]) -> Netlist:
    """Inputs trst_n (2), en (3), rst_n (4), clk (5); the cells given."""
    ports = {"trst_n": 2, "en": 3, "rst_n": 4, "clk": 5}
    module = {
        "ports": {n: {"direction": "input", "bits": [b]} for n, b in ports.items()},
        "cells": cells,
    }
    return Netlist(module, CELL_MAP)


@pytest.mark.unit
def test_buffers_and_inverters_carry_a_held_value_to_the_pin() -> None:
    net = _netlist(
        i0=_cell("inv_1", A=2, Y=10),
        b0=_cell("buf_1", A=10, X=11),
        f0=_cell("dfrtp_1", CLK=5, D=3, RESET_B=11, Q=12),
    )
    found = net.forced("f0", "RESET_B", {"trst_n": 1})
    assert found is not None and found.value == 0
    assert found.steps == (
        (11, "f0", "RESET_B", 0),
        (10, "b0", "A", 0),
        (2, "i0", "A", 1),
    )
    assert release_faults(found) == {
        ("net:11:stem", "sa1"),
        ("net:11:branch:f0:RESET_B", "sa1"),
        ("net:10:stem", "sa1"),
        ("net:10:branch:b0:A", "sa1"),
        ("net:2:stem", "sa0"),
        ("net:2:branch:i0:A", "sa0"),
    }
    assert net.forced("f0", "RESET_B", {}) is None  # trst_n not held


@pytest.mark.unit
def test_a_gate_is_set_by_one_input_at_its_controlling_value() -> None:
    """trst_n & en clears f0 with trst_n at 0, whatever en is: the path runs through
    trst_n, not en. At 1 it sets nothing."""
    net = _netlist(
        a0=_cell("and2_1", A=2, B=3, X=10),
        f0=_cell("dfrtp_1", CLK=5, D=3, RESET_B=10, Q=12),
    )
    found = net.forced("f0", "RESET_B", {"trst_n": 0})
    assert found is not None and found.value == 0
    assert found.steps == ((10, "f0", "RESET_B", 0), (2, "a0", "A", 0))
    assert net.forced("f0", "RESET_B", {"trst_n": 1}) is None
    assert net.forced("f0", "RESET_B", {"trst_n": 1, "en": 1}) is not None


@pytest.mark.unit
def test_an_inverting_gate_flips_and_two_controlling_inputs_both_count() -> None:
    """nand2(trst_n, en) is 1 with either at 0; with both at 0, a stuck-at on one
    can't release it, but both paths are listed (conservative)."""
    net = _netlist(
        n0=_cell("nand2_1", A=2, B=3, Y=10),
        f0=_cell("dfstp_1", CLK=5, D=3, SET_B=10, Q=12),
    )
    one = net.forced("f0", "SET_B", {"trst_n": 0, "en": 1})
    assert one is not None and one.value == 1
    assert {s[0] for s in one.steps} == {10, 2}
    both = net.forced("f0", "SET_B", {"trst_n": 0, "en": 0})
    assert both is not None and {s[0] for s in both.steps} == {10, 2, 3}


@pytest.mark.unit
def test_a_bubbled_input_is_set_by_the_opposite_value() -> None:
    """and2b's A_N (its second input, gate_eval's in1) forces 0 at 1."""
    cell = _cell("and2b_1", B=3, A_N=2, X=10)
    net = _netlist(a0=cell, f0=_cell("dfrtp_1", CLK=5, D=3, RESET_B=10, Q=12))
    assert net.forced("f0", "RESET_B", {"trst_n": 1}) is not None
    assert net.forced("f0", "RESET_B", {"trst_n": 0}) is None


@pytest.mark.unit
def test_known_flop_outputs_and_constants_hold_a_pin_and_other_logic_does_not() -> None:
    net = _netlist(
        s0=_cell("dfrtp_1", CLK=5, D="1", RESET_B=4, Q=10),
        c0=_cell("dfrtp_1", CLK=5, D=3, RESET_B=10, Q=11),
        a0=_cell("and2_1", A="0", B=3, X=12),
        c1=_cell("dfrtp_1", CLK=5, D=3, RESET_B=12, Q=13),
        x0=_cell("xor2_1", A=4, B=3, X=14),
        c2=_cell("dfrtp_1", CLK=5, D=3, RESET_B=14, Q=15),
    )
    assert net.forced("c0", "RESET_B", {}) is None  # s0's output: a flop
    known = net.forced("c0", "RESET_B", {}, known={10: 0})
    assert known is not None and known.steps == ((10, "c0", "RESET_B", 0),)
    tied = net.forced("c1", "RESET_B", {})
    assert tied is not None and tied.value == 0
    assert tied.steps == ((12, "c1", "RESET_B", 0),)  # a constant bit has no site
    assert net.forced("c2", "RESET_B", {"rst_n": 0, "en": 0}) is None  # xor
