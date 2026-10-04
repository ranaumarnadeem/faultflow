"""The BIST program's pieces, without a simulator: the procedure a schedule
gives, the TCK latency padding, the vector format, and the clock a memory runs on.
test_bist_program.py plays whole programs against the inserted RTL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from faultflow.mbist.insert import _program_memory, _same_module
from faultflow.mbist.netlist import InsertError
from faultflow.mbist.program import (
    CLK_TERMS,
    STATUS_SYNC,
    TDR_CLEAR,
    ProgramMemory,
    Vector,
    _padded,
    clock_port,
    render_vectors,
    run_steps,
)

pytestmark = pytest.mark.unit


def _memory(name: str, bound: int = 100, clock: str = "clk") -> ProgramMemory:
    ports = ("test_mode", "bist_start", "bist_done", "bist_fail")
    return ProgramMemory(name, {p: f"{name}_{p}" for p in ports}, clock, bound)


def _commands(steps: list[Any]) -> list[str]:
    return [
        (
            f"{s.kind} {s.instrument} {s.value}".strip()
            if s.kind in ("write", "read")
            else (f"runloop {s.count} {s.clock}" if s.kind == "runloop" else s.kind)
        )
        for s in steps
    ]


def test_a_step_writes_test_mode_then_start_runs_then_reads_done_then_fail() -> None:
    commands = _commands(run_steps([_memory("a")], [["a"]]))
    assert commands == [
        "write a_test_mode 1", "apply",
        "write a_bist_start 1", "apply",
        f"runloop {100 + CLK_TERMS} clk",
        "read a_bist_done 1", "apply",
        "read a_bist_fail 0", "apply",
        "write a_bist_start 0", "apply",
        "write a_test_mode 0", "apply",
    ]  # fmt: skip


def test_a_step_of_two_starts_both_then_runs_once_for_the_longer() -> None:
    commands = _commands(run_steps([_memory("a", 50), _memory("b", 80)], [["a", "b"]]))
    assert commands[:9] == [
        "write a_test_mode 1", "apply", "write b_test_mode 1", "apply",
        "write a_bist_start 1", "apply", "write b_bist_start 1", "apply",
        f"runloop {80 + CLK_TERMS} clk",
    ]  # fmt: skip
    assert sum(c.startswith("runloop") for c in commands) == 1
    reads = [c for c in commands if c.startswith("read")]
    assert reads == [
        "read a_bist_done 1", "read b_bist_done 1",
        "read a_bist_fail 0", "read b_bist_fail 0",
    ]  # fmt: skip


def test_steps_run_one_after_another() -> None:
    commands = _commands(
        run_steps([_memory("a", 50), _memory("b", 80)], [["b"], ["a"]])
    )
    loops = [c for c in commands if c.startswith("runloop")]
    assert loops == [f"runloop {80 + CLK_TERMS} clk", f"runloop {50 + CLK_TERMS} clk"]
    assert commands.index("write a_test_mode 1") > commands.index("write b_test_mode 0")


def test_a_step_mixing_clocks_is_refused() -> None:
    with pytest.raises(InsertError, match="one run loop counts one clock"):
        run_steps([_memory("a"), _memory("b", clock="clk2")], [["a", "b"]])


def test_a_collar_without_the_ports_the_program_uses_is_refused() -> None:
    memory = ProgramMemory("a", {"test_mode": "a_test_mode"}, "clk", 10)
    with pytest.raises(InsertError, match="bist_start"):
        run_steps([memory], [["a"]])


# --- the TCK latency terms --------------------------------------------------------


class _State:
    """Stand-ins for the TAP states _padded looks at."""

    RUN_TEST_IDLE = "idle"
    CAPTURE_DR = "capture"
    UPDATE_DR = "update"


def _tck(state: str) -> Vector:
    return Vector("tck", state=state)


def test_an_update_dr_too_close_to_the_start_gets_idle_cycles_before_it() -> None:
    """One edge comes before the Update-DR; TDR_CLEAR are needed."""
    vectors = [_tck("idle"), _tck("update"), _tck("idle")]
    padded = _padded(vectors, _State)
    added = TDR_CLEAR - 1
    assert [v.state for v in padded] == ["idle"] * added + ["idle", "update", "idle"]
    assert all(v.tms == 0 for v in padded[:added])


def test_a_capture_too_close_to_a_run_gets_idle_cycles_after_the_run() -> None:
    vectors = [_tck("x")] * TDR_CLEAR + [_tck("update"), Vector("run", count=5)]
    vectors += [_tck("capture"), _tck("y")]
    padded = _padded(vectors, _State)
    run = next(i for i, v in enumerate(padded) if v.kind == "run")
    assert [v.state for v in padded[run + 1 :]] == ["idle"] * STATUS_SYNC + [
        "capture",
        "y",
    ]


def test_a_walk_long_enough_is_left_alone() -> None:
    vectors = [_tck("a"), _tck("b"), _tck("update"), Vector("run", count=3)]
    vectors += [_tck("a"), _tck("b"), _tck("capture")]
    assert _padded(vectors, _State) == vectors


def test_vectors_are_one_line_each() -> None:
    vectors = [
        Vector("tck", tms=1, tdi=0),
        Vector("tck", tms=0, tdi=1, tdo=1, read=3),
        Vector("run", count=426, clock="clk"),
    ]
    assert render_vectors(vectors, ["clk"]) == "0 1 0 0 0\n0 0 1 1 3\n1 426 0 0 0\n"


# --- the clock a memory runs on -------------------------------------------------------


def _modules(gated: bool) -> dict[str, Any]:
    """top -> u_core (core) -> the memory's shell, clocked from the top's clk, or
    from a gate inside the core."""
    core_clk = [20] if not gated else [21]
    return {
        "top": {
            "ports": {"clk": {"direction": "input", "bits": [2]}},
            "cells": {"u_core": {"type": "core", "connections": {"clk": [2]}}},
        },
        "core": {
            "ports": {"clk": {"direction": "input", "bits": [20]}},
            "cells": {"u_mem": {"type": "shell", "connections": {"clk": core_clk}}},
        },
    }


def test_the_clock_is_followed_up_to_a_chip_input() -> None:
    chain = [("top", "u_core"), ("core", "u_mem")]
    assert clock_port(_modules(gated=False), chain, [20]) == "clk"


def test_a_clock_from_logic_has_no_chip_input() -> None:
    chain = [("top", "u_core"), ("core", "u_mem")]
    assert clock_port(_modules(gated=True), chain, [21]) is None


# --- one module elaborated twice ---------------------------------------------------


def _module(tmp_path: Path, text: str, cells: dict[str, Any]) -> dict[str, Any]:
    source = tmp_path / f"m{len(list(tmp_path.iterdir()))}.v"
    source.write_text(text, encoding="utf-8")
    return {
        "attributes": {"src": f"{source}:1.1-3.10"},
        "parameter_default_values": {"WIDTH": "1"},
        "cells": cells,
    }


def test_one_source_elaborated_twice_is_one_module(tmp_path: Path) -> None:
    """Yosys names what it makes up per run ($procdff$12 vs $procdff$47): the
    same source and parameters is the same module, whatever those names."""
    text = "module m;\n  // body\nendmodule\n"
    a = _module(tmp_path, text, {"$procdff$12": {"type": "$adff"}})
    b = _module(tmp_path, text, {"$procdff$47": {"type": "$adff"}})
    assert _same_module(a, b)
    other = _module(tmp_path, "module m;\n  // other\nendmodule\n", {})
    assert not _same_module(a, other)
    b["parameter_default_values"] = {"WIDTH": "2"}
    assert not _same_module(a, b)


@pytest.mark.parametrize(
    ("clock", "bound", "match"),
    [(None, 420, "clock comes from logic"), ("clk", None, "MBIST_MAX_CYCLES")],
)
def test_a_memory_the_program_cant_run_is_refused(
    clock: str | None, bound: int | None, match: str
) -> None:
    with pytest.raises(InsertError, match=match):
        _program_memory("a", {}, clock, bound)
