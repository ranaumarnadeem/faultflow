"""The setup program's three-valued simulation (faultflow.scan.setup_sim): what a test
setup program leaves in a flop, whatever the chip powered up in.

Checked against the C++ core, which grades from every flop at one value: a value the
three-valued run calls known must be what the core finds from all-0 and from all-1,
with the test's free inputs at any value."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from faultflow.control_trace import Netlist
from faultflow.scan.setup import SetupCycle, setup_test_cycles
from faultflow.scan.setup_sim import SetupSimulator, changed_by

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
TOP = "cfg_chip"
MUX = "sky130_fd_sc_hd__mux2_1"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"
DFBBN = "sky130_fd_sc_hd__dfbbn_1"
OUTPUTS = ("Q", "X", "Y")


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in OUTPUTS else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _module(
    inputs: dict[str, int], outputs: dict[str, int], cells: dict[str, Any]
) -> dict[str, Any]:
    ports = {n: {"direction": "input", "bits": [b]} for n, b in inputs.items()}
    ports.update({n: {"direction": "output", "bits": [b]} for n, b in outputs.items()})
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {
                    name: {"hide_name": 0, "bits": [bit], "attributes": {}}
                    for name, bit in {**inputs, **outputs}.items()
                },
            }
        }
    }


# clk 2, en 3, din 4, upd 5, rst_n 6, set_n 7. sh (dfxtp on clk) loads din when en,
# else holds; up (dfbbn: the falling edge of clk) loads sh when upd, else holds, and
# rst_n clears it. sh_out = sh, up_out = up.
INPUTS = {"clk": 2, "en": 3, "din": 4, "upd": 5, "rst_n": 6, "set_n": 7}
REGISTER = _module(
    INPUTS,
    {"sh_out": 10, "up_out": 12},
    {
        "m_sh": _cell(MUX, A0=10, A1=4, S=3, X=20),
        "sh": _cell(DFXTP, CLK=2, D=20, Q=10),
        "m_up": _cell(MUX, A0=12, A1=10, S=5, X=21),
        "up": _cell(DFBBN, CLK_N=2, D=21, RESET_B=6, SET_B=7, Q=12),
    },
)
CLOCKS = {"clk": 0}
HOLDS = {"en": 0, "upd": 0, "rst_n": 1, "set_n": 1}


def _program(*cycles: tuple[dict[str, int], bool]) -> tuple[SetupCycle, ...]:
    return tuple(
        SetupCycle(tuple(sorted(inputs.items())), ("clk",) if pulse else ())
        for inputs, pulse in cycles
    )


LOAD_THEN_UPDATE = _program(
    ({"rst_n": 0, "set_n": 1}, False),
    ({"rst_n": 1, "set_n": 1, "en": 1, "din": 1}, True),
    ({"rst_n": 1, "set_n": 1, "upd": 1}, True),
)


def _sim(design: dict[str, Any], targets: tuple[str, ...]) -> SetupSimulator:
    return SetupSimulator(Netlist(design["modules"][TOP], CELL_MAP), targets)


def _core_values(
    tmp_path: Path,
    design: dict[str, Any],
    program: tuple[SetupCycle, ...],
    clocks: dict[str, int],
    holds: dict[str, int],
    outputs: list[str],
    free_value: int,
    initial: bool,
) -> list[bool]:
    """The outputs after the program and the test's first step, by the C++ core:
    every flop starting at `initial`, the test's free inputs at `free_value`."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    path = tmp_path / "chip.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    names = list(design["modules"][TOP]["ports"])
    order = [
        n for n in names if design["modules"][TOP]["ports"][n]["direction"] == "input"
    ]
    steps = setup_test_cycles(program, clocks)
    final = {n: free_value for n in order}
    final.update(holds)
    steps.append((final, dict(clocks)))
    vectors = [
        [bool({**inputs, **levels}.get(name, 0)) for name in order]
        for inputs, levels in steps
    ]
    result = core.simulate_sequence_faults(
        str(path),
        str(CELL_MAP_PATH),
        order,
        vectors,
        [False] * (len(vectors) - 1) + [True],
        outputs,
        [],
        initial_ff_value=initial,
    )
    return [bool(v) for v in result["golden"][-1]]


@pytest.mark.unit
def test_a_loaded_register_is_known_whatever_the_power_up(
    tmp_path: Path, require_cpp_core: None
) -> None:
    sim = _sim(REGISTER, ("sh", "up"))
    found, known = sim.run(LOAD_THEN_UPDATE, CLOCKS, HOLDS)
    # up updated on the falling edge of the pulse that had upd at 1.
    assert found == {"sh": 1, "up": 1}
    assert {2, 3, 4, 5, 6, 7, 10, 12} <= known
    for initial in (False, True):
        for free in (0, 1):
            assert _core_values(
                tmp_path,
                REGISTER,
                LOAD_THEN_UPDATE,
                CLOCKS,
                HOLDS,
                ["sh_out", "up_out"],
                free,
                initial,
            ) == [True, True]


@pytest.mark.unit
def test_what_the_program_doesnt_set_is_unknown(
    tmp_path: Path, require_cpp_core: None
) -> None:
    # No load: sh keeps its power-up value, and up takes it.
    update_only = _program(({"rst_n": 1, "set_n": 1, "upd": 1}, True))
    found, _ = _sim(REGISTER, ("sh", "up")).run(update_only, CLOCKS, HOLDS)
    assert found == {"sh": None, "up": None}
    seen = {
        tuple(
            _core_values(
                tmp_path,
                REGISTER,
                update_only,
                CLOCKS,
                HOLDS,
                ["sh_out", "up_out"],
                0,
                initial,
            )
        )
        for initial in (False, True)
    }
    assert seen == {(False, False), (True, True)}
    # Nothing held: the test's first step pulses no clock, so sh keeps what the
    # program loaded (holding it beyond that step is nonscan.py's proof).
    found, _ = _sim(REGISTER, ("sh",)).run(LOAD_THEN_UPDATE, CLOCKS, {})
    assert found == {"sh": 1}


@pytest.mark.unit
def test_an_edge_into_the_test_is_seen() -> None:
    # The program ends with clk... clk is a scan clock and always ends off. A
    # level clock the program leaves high falls into the test when the test holds
    # it low: a falling-edge flop on it takes its D.
    design = _module(
        {"tck": 2, "d": 4, "rst_n": 6},
        {"q_out": 12},
        {"r": _cell(DFBBN, CLK_N=2, D=4, RESET_B=6, SET_B="1", Q=12)},
    )
    program = _program(({"rst_n": 0}, False), ({"rst_n": 1, "tck": 1}, False))
    sim = SetupSimulator(Netlist(design["modules"][TOP], CELL_MAP), ("r",))
    assert sim.run(program, {}, {"rst_n": 1, "tck": 1})[0] == {"r": 0}
    # tck falls into the test with d free: unknown.
    assert sim.run(program, {}, {"rst_n": 1, "tck": 0})[0] == {"r": None}
    # ... with d held at 1: 1.
    assert sim.run(program, {}, {"rst_n": 1, "tck": 0, "d": 1})[0] == {"r": 1}


@pytest.mark.unit
def test_a_stuck_at_that_changes_the_program_is_found(require_cpp_core: None) -> None:
    sim = _sim(REGISTER, ("sh", "up"))
    expected, _ = sim.run(LOAD_THEN_UPDATE, CLOCKS, HOLDS)
    # din stuck at 0: sh loads 0.
    assert changed_by(
        sim, LOAD_THEN_UPDATE, CLOCKS, HOLDS, expected, (4, None, None, 0)
    )
    # din stuck at 1: what the program loads anyway.
    assert not changed_by(
        sim, LOAD_THEN_UPDATE, CLOCKS, HOLDS, expected, (4, None, None, 1)
    )
    # upd's branch into m_up stuck at 0: up never updates and keeps its reset 0.
    assert changed_by(
        sim, LOAD_THEN_UPDATE, CLOCKS, HOLDS, expected, (5, "m_up", "S", 0)
    )
    # rst_n stuck at 1: up isn't reset, but the update loads it anyway.
    assert not changed_by(
        sim, LOAD_THEN_UPDATE, CLOCKS, HOLDS, expected, (6, None, None, 1)
    )


GATES = (
    ("sky130_fd_sc_hd__inv_1", ("A",), ("Y",)),
    ("sky130_fd_sc_hd__nand2_1", ("A", "B"), ("Y",)),
    ("sky130_fd_sc_hd__nor2_1", ("A", "B"), ("Y",)),
    ("sky130_fd_sc_hd__xor2_1", ("A", "B"), ("X",)),
    ("sky130_fd_sc_hd__and2b_1", ("A_N", "B"), ("X",)),
    (MUX, ("A0", "A1", "S"), ("X",)),
    ("sky130_fd_sc_hd__a21oi_1", ("A1", "A2", "B1"), ("Y",)),
    ("sky130_fd_sc_hd__fa_1", ("A", "B", "CIN"), ("SUM", "COUT")),
    ("sky130_fd_sc_hd__ha_1", ("A", "B"), ("SUM", "COUT")),
)


def _random_chip(rng: random.Random) -> tuple[dict[str, Any], list[str]]:
    """Inputs clk 2 and i0.. i4 3..7; a few gates and flops over them; each flop
    on an output port of its own."""
    inputs = {"clk": 2, **{f"i{k}": 3 + k for k in range(5)}}
    nets = list(range(3, 8))
    cells: dict[str, Any] = {}
    flops: list[str] = []
    next_net = 20
    pending_q: list[tuple[str, int]] = []
    for k in range(rng.randint(2, 5)):
        pending_q.append((f"f{k}", next_net))
        nets.append(next_net)
        next_net += 1
    for k in range(rng.randint(3, 10)):
        kind, pins, outs = rng.choice(GATES)
        conns: dict[str, int | str] = {p: rng.choice(nets) for p in pins}
        for out in outs:
            conns[out] = next_net
            nets.append(next_net)
            next_net += 1
        cell = _cell(kind, **conns)
        cell["port_directions"].update(dict.fromkeys(outs, "output"))
        cells[f"g{k}"] = cell
    outputs: dict[str, int] = {}
    for name, q in pending_q:
        kind = rng.choice((DFXTP, DFRTP, DFBBN))
        d = rng.choice(nets)
        if kind == DFXTP:
            cells[name] = _cell(kind, CLK=2, D=d, Q=q)
        elif kind == DFRTP:
            cells[name] = _cell(kind, CLK=2, D=d, RESET_B=rng.choice(nets), Q=q)
        else:
            cells[name] = _cell(
                kind, CLK_N=2, D=d, RESET_B=rng.choice(nets), SET_B="1", Q=q
            )
        outputs[f"{name}_out"] = q
        flops.append(name)
    return _module(inputs, outputs, cells), flops


@pytest.mark.unit
def test_known_values_agree_with_the_core_on_random_chips(
    tmp_path: Path, require_cpp_core: None
) -> None:
    rng = random.Random(1500)
    checked = 0
    for trial in range(40):
        design, flops = _random_chip(rng)
        program = tuple(
            SetupCycle(
                tuple(sorted({f"i{k}": rng.randint(0, 1) for k in range(5)}.items())),
                ("clk",) if rng.random() < 0.7 else (),
            )
            for _ in range(rng.randint(1, 6))
        )
        holds = {f"i{k}": rng.randint(0, 1) for k in range(5) if rng.random() < 0.6}
        found, _ = _sim(design, tuple(flops)).run(program, CLOCKS, holds)
        outputs = [f"{name}_out" for name in flops]
        for initial in (False, True):
            for free in (0, 1):
                core = _core_values(
                    tmp_path, design, program, CLOCKS, holds, outputs, free, initial
                )
                for name, value in zip(flops, core):
                    if found[name] is not None:
                        assert int(value) == found[name], (trial, name)
                        checked += 1
    assert checked > 100
