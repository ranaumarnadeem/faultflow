"""Reproducible SAT abort limits: [atpg] sat_conflict_schedule.

With a conflict schedule each SAT tier is a CaDiCaL conflict cap (load-independent)
and the wall clock is only a backstop (sat_ceiling_seconds), so a run gives the same
patterns on any machine under any load. Without one, the tiers stay exactly as
today: (sat_conflict_limit, each sat_timeout_schedule entry).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from campaign_fixtures import campaign_id_for_cfg
from faultflow.config import (
    AtpgConfig,
    ConfigError,
    SatTier,
    load_config,
    parse_conflict_schedule,
    sat_tiers,
)
from faultflow.runner.progressive_atpg import (
    redundancy_model_id,
    run_progressive_native_atpg,
    run_progressive_transition_atpg,
)

ROOT = Path(__file__).resolve().parents[3]
TINY_INV_JSON = ROOT / "tests/cpp/fixtures/tiny_inv.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

# Positional (conflict_limit, sat_timeout_seconds) of core.solve_fault_atpg and
# core.solve_transition_fault_atpg: (json, cell_map, db, fault_id, blocked, ...).
_FAULT_ID_ARG = 3
_CONFLICT_ARG = 5
_TIMEOUT_ARG = 6
# solve_fault_worker's tuple: (kind, json, cell_map, db, fault_id, blocked,
# conflict_limit, timeout, ...).
_WAVE_FAULT_ID = 4
_WAVE_CONFLICT = 6
_WAVE_TIMEOUT = 7


# --------------------------------------------------------------------------- #
# parse_conflict_schedule / sat_tiers -- pure functions
# --------------------------------------------------------------------------- #


def test_empty_conflict_schedule_is_off() -> None:
    assert parse_conflict_schedule("") == []
    assert parse_conflict_schedule("   ") == []


def test_conflict_schedule_parses_comma_list_in_order() -> None:
    assert parse_conflict_schedule("2000,20000,200000") == [2000, 20000, 200000]
    assert parse_conflict_schedule(" 5 , 50 ") == [5, 50]


def test_conflict_schedule_rejects_bad_entries() -> None:
    with pytest.raises(ConfigError, match="must be integers"):
        parse_conflict_schedule("2000,many")
    with pytest.raises(ConfigError, match=">= 1 conflict"):
        parse_conflict_schedule("0,10")
    with pytest.raises(ConfigError, match=">= 1 conflict"):
        parse_conflict_schedule("-5")
    # Same rule as the wall-clock schedule: a conflict-capped solve is
    # deterministic, so an equal or smaller next cap can never resolve anything.
    with pytest.raises(ConfigError, match="strictly increasing"):
        parse_conflict_schedule("200,100")
    with pytest.raises(ConfigError, match="strictly increasing"):
        parse_conflict_schedule("100,100")


DEFAULT_TIERS = [
    SatTier(conflicts=10000, seconds=300),
    SatTier(conflicts=100000, seconds=300),
    SatTier(conflicts=1000000, seconds=300),
]
WALL_CLOCK_TIERS = [
    SatTier(conflicts=100000, seconds=2),
    SatTier(conflicts=100000, seconds=10),
    SatTier(conflicts=100000, seconds=60),
]


def test_default_tiers_are_the_conflict_schedule() -> None:
    # The default is reproducible: three conflict caps under the 300 s backstop.
    assert AtpgConfig().sat_conflict_schedule == "10000,100000,1000000"
    assert sat_tiers(AtpgConfig()) == DEFAULT_TIERS


def test_wall_clock_tiers_are_todays_values() -> None:
    atpg = replace(AtpgConfig(), sat_conflict_schedule="")
    assert sat_tiers(atpg) == [
        SatTier(conflicts=100000, seconds=2),
        SatTier(conflicts=100000, seconds=10),
        SatTier(conflicts=100000, seconds=60),
    ]
    fixed = replace(atpg, sat_timeout_schedule="", sat_timeout_seconds=7)
    assert sat_tiers(fixed) == [SatTier(conflicts=100000, seconds=7)]
    capped = replace(atpg, sat_conflict_limit=500)
    assert [t.conflicts for t in sat_tiers(capped)] == [500, 500, 500]


def test_conflict_tiers_use_the_ceiling() -> None:
    atpg = replace(AtpgConfig(), sat_conflict_schedule="10,20", sat_ceiling_seconds=99)
    assert sat_tiers(atpg) == [
        SatTier(conflicts=10, seconds=99),
        SatTier(conflicts=20, seconds=99),
    ]


# --------------------------------------------------------------------------- #
# [atpg] keys
# --------------------------------------------------------------------------- #


def _write_cfg(tmp_path: Path, atpg_lines: str) -> Path:
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {TINY_INV_JSON}
cell_lib = {CELL_LIB}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
{atpg_lines}

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg_path


def test_config_default_is_the_conflict_schedule(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path, "random_vectors = 0"), top="tiny_inv")
    assert cfg.atpg.sat_conflict_schedule == "10000,100000,1000000"
    assert sat_tiers(cfg.atpg) == DEFAULT_TIERS


@pytest.mark.parametrize(
    "lines, tiers",
    [
        # An existing config that tunes the wall clock keeps it, as it ran before
        # the conflict schedule became the default.
        ("sat_timeout_schedule = 2,10,60", WALL_CLOCK_TIERS),
        (
            "sat_conflict_limit = 5000",
            [SatTier(5000, 2), SatTier(5000, 10), SatTier(5000, 60)],
        ),
        ("sat_timeout_schedule =\nsat_timeout_seconds = 7", [SatTier(100000, 7)]),
        # An explicitly empty conflict schedule turns it off.
        ("sat_conflict_schedule =", WALL_CLOCK_TIERS),
    ],
)
def test_an_explicit_wall_clock_key_selects_the_wall_clock(
    tmp_path: Path, lines: str, tiers: list[SatTier]
) -> None:
    cfg = load_config(_write_cfg(tmp_path, lines), top="tiny_inv")
    assert cfg.atpg.sat_conflict_schedule == ""
    assert sat_tiers(cfg.atpg) == tiers


def test_sat_timeout_seconds_alone_keeps_the_conflict_schedule(tmp_path: Path) -> None:
    # sat_timeout_seconds is only the fallback for an empty sat_timeout_schedule
    # (a no-op under the old 2,10,60 default too); the flowscripts set it always.
    cfg = load_config(_write_cfg(tmp_path, "sat_timeout_seconds = 30"), top="tiny_inv")
    assert sat_tiers(cfg.atpg) == DEFAULT_TIERS


def test_config_reads_the_conflict_schedule(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path,
            "sat_conflict_schedule = 2000, 20000,200000\nsat_ceiling_seconds = 120",
        ),
        top="tiny_inv",
    )
    assert sat_tiers(cfg.atpg) == [
        SatTier(2000, 120),
        SatTier(20000, 120),
        SatTier(200000, 120),
    ]


@pytest.mark.parametrize(
    "lines, message",
    [
        ("sat_conflict_schedule = 20,10", "strictly increasing"),
        ("sat_ceiling_seconds = 0", "sat_ceiling_seconds"),
        # The wall-clock keys don't apply with a conflict schedule: setting both
        # is a contradiction, not something to resolve silently.
        (
            "sat_conflict_schedule = 10,20\nsat_timeout_schedule = 2,10",
            "sat_timeout_schedule",
        ),
        (
            "sat_conflict_schedule = 10,20\nsat_conflict_limit = 5000",
            "sat_conflict_limit",
        ),
    ],
)
def test_config_rejects(tmp_path: Path, lines: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(_write_cfg(tmp_path, lines), top="tiny_inv")


# --------------------------------------------------------------------------- #
# The combinational loops pass each tier's pair and escalate through the caps
# --------------------------------------------------------------------------- #


def _model_id(fault_model: str = "stuck_at") -> str:
    return redundancy_model_id(
        {
            "netlist_hash": "x",
            "cell_lib_hash": "y",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
            "fault_model": fault_model,
        }
    )


def _tiny_cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transition: bool = False,
    workers: int = 1,
) -> tuple[Any, Path]:
    if not TINY_INV_JSON.exists():
        pytest.skip("tiny_inv fixture missing")
    model = "model = transition\nlaunch = loc\n" if transition else ""
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {TINY_INV_JSON}
cell_lib = {CELL_LIB}

[fault_model]
{model}collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 0
max_rounds = 20
compaction = none
workers = {workers}
sat_conflict_schedule = 10,20,30
sat_ceiling_seconds = 77

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config(cfg_path, top="tiny_inv")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    netlist = cfg.output_dir / "tiny_inv.json"
    shutil.copy(TINY_INV_JSON, netlist)
    return cfg, netlist


def _core(monkeypatch: pytest.MonkeyPatch) -> Any:
    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")
    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(core, "atpg_random_vector_pairs", lambda *_a, **_k: [])
    return core


def _recording_solve(
    seen: list[tuple[int, int, int]], *, first_call_seconds: float = 0.0
) -> Any:
    """A solve that records (fault_id, conflict cap, wall-clock limit) and
    aborts: TIMEOUT, as the C++ reports any limit stop. Instant, it is a
    conflict-cap stop; `first_call_seconds` makes the first call run that long
    first, a wall-clock stop when it reaches the limit."""

    def solve(*args: Any, **kwargs: Any) -> dict[str, object]:
        if not seen and first_call_seconds:
            time.sleep(first_call_seconds)
        seen.append(
            (
                int(args[_FAULT_ID_ARG]),
                int(kwargs.get("conflict_limit", args[_CONFLICT_ARG])),
                int(kwargs.get("sat_timeout_seconds", args[_TIMEOUT_ARG])),
            )
        )
        return {"result": "TIMEOUT", "vector": {}, "launch": {}, "capture": {}}

    return solve


class _InlinePool:
    """Stands in for ProcessPoolExecutor: runs nothing, records each wave's
    solve_fault_worker tuples and answers TIMEOUT after `solve_seconds`."""

    waves: list[tuple[Any, ...]] = []
    solve_seconds = 0.0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def map(self, _fn: Any, wave: Any) -> list[tuple[int, str, dict[str, object]]]:
        out = []
        for args in wave:
            _InlinePool.waves.append(tuple(args))
            out.append(
                (
                    int(args[_WAVE_FAULT_ID]),
                    "TIMEOUT",
                    {"result": "TIMEOUT", "solve_seconds": _InlinePool.solve_seconds},
                )
            )
        return out

    def shutdown(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def _per_fault(seen: list[tuple[int, int, int]]) -> dict[int, list[tuple[int, int]]]:
    out: dict[int, list[tuple[int, int]]] = {}
    for fault_id, conflicts, seconds in seen:
        out.setdefault(fault_id, []).append((conflicts, seconds))
    return out


def _assert_escalates(per_fault: dict[int, list[tuple[int, int]]]) -> None:
    escalating = [seq for seq in per_fault.values() if len(seq) >= 3]
    assert escalating, "no fault was attempted in three rounds"
    for seq in escalating:
        # Smallest cap first, the next one only after an abort, capped at the
        # last; the wall clock is the ceiling on every attempt.
        assert seq[:3] == [(10, 77), (20, 77), (30, 77)]
        assert all(pair == (30, 77) for pair in seq[3:])


@pytest.mark.unit
def test_stuck_at_loop_escalates_through_conflict_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch)
    core = _core(monkeypatch)
    seen: list[tuple[int, int, int]] = []
    monkeypatch.setattr(core, "solve_fault_atpg", _recording_solve(seen))

    run_progressive_native_atpg(
        cfg, netlist, _model_id(), campaign_id=campaign_id_for_cfg(cfg, netlist)
    )

    _assert_escalates(_per_fault(seen))


@pytest.mark.unit
def test_transition_loop_escalates_through_conflict_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch, transition=True)
    core = _core(monkeypatch)
    seen: list[tuple[int, int, int]] = []
    monkeypatch.setattr(core, "solve_transition_fault_atpg", _recording_solve(seen))

    run_progressive_transition_atpg(
        cfg,
        netlist,
        _model_id("transition"),
        campaign_id=campaign_id_for_cfg(cfg, netlist),
    )

    _assert_escalates(_per_fault(seen))


@pytest.mark.unit
@pytest.mark.parametrize("transition", [False, True])
def test_parallel_waves_carry_the_tier_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: bool
) -> None:
    import faultflow.runner.progressive_atpg as progressive

    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch, transition=transition, workers=2)
    _core(monkeypatch)
    _InlinePool.waves = []
    _InlinePool.solve_seconds = 0.0
    monkeypatch.setattr(progressive, "ProcessPoolExecutor", _InlinePool)

    run = run_progressive_transition_atpg if transition else run_progressive_native_atpg
    run(
        cfg,
        netlist,
        _model_id("transition" if transition else "stuck_at"),
        campaign_id=campaign_id_for_cfg(cfg, netlist),
    )

    seen = [
        (int(a[_WAVE_FAULT_ID]), int(a[_WAVE_CONFLICT]), int(a[_WAVE_TIMEOUT]))
        for a in _InlinePool.waves
    ]
    _assert_escalates(_per_fault(seen))


def _ceiling_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.message
        for r in caplog.records
        if r.levelname == "WARNING" and "sat_ceiling_seconds" in r.message
    ]


@pytest.mark.unit
@pytest.mark.parametrize("transition", [False, True])
def test_a_ceiling_hit_warns_the_run_may_not_reproduce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    transition: bool,
) -> None:
    """A solve the wall-clock ceiling stopped (it ran the whole ceiling) has a
    verdict that depends on load: the run says so, with the count."""
    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch, transition=transition)
    cfg = replace(cfg, atpg=replace(cfg.atpg, sat_ceiling_seconds=1))
    core = _core(monkeypatch)
    seen: list[tuple[int, int, int]] = []
    name = "solve_transition_fault_atpg" if transition else "solve_fault_atpg"
    monkeypatch.setattr(core, name, _recording_solve(seen, first_call_seconds=1.05))
    run = run_progressive_transition_atpg if transition else run_progressive_native_atpg

    with caplog.at_level("WARNING"):
        run(
            cfg,
            netlist,
            _model_id("transition" if transition else "stuck_at"),
            campaign_id=campaign_id_for_cfg(cfg, netlist),
            max_rounds=1,
        )

    assert len(seen) > 1
    warnings = _ceiling_warnings(caplog)
    assert len(warnings) == 1
    assert " 1 SAT solve(s) " in warnings[0] and "reproduce" in warnings[0]


@pytest.mark.unit
def test_a_parallel_ceiling_hit_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """In a parallel wave the worker's own solve time decides."""
    import faultflow.runner.progressive_atpg as progressive

    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch, workers=2)
    cfg = replace(cfg, atpg=replace(cfg.atpg, sat_ceiling_seconds=1))
    _core(monkeypatch)
    _InlinePool.waves = []
    _InlinePool.solve_seconds = 1.5
    monkeypatch.setattr(progressive, "ProcessPoolExecutor", _InlinePool)

    with caplog.at_level("WARNING"):
        run_progressive_native_atpg(
            cfg,
            netlist,
            _model_id(),
            campaign_id=campaign_id_for_cfg(cfg, netlist),
            max_rounds=1,
        )

    warnings = _ceiling_warnings(caplog)
    assert len(warnings) == 1
    assert _InlinePool.waves
    assert f" {len(_InlinePool.waves)} SAT solve(s) " in warnings[0]


@pytest.mark.unit
def test_no_ceiling_warning_for_conflict_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An instant TIMEOUT is the conflict cap, load-independent: no warning."""
    cfg, netlist = _tiny_cfg(tmp_path, monkeypatch)
    core = _core(monkeypatch)
    seen: list[tuple[int, int, int]] = []
    monkeypatch.setattr(core, "solve_fault_atpg", _recording_solve(seen))

    with caplog.at_level("WARNING"):
        run_progressive_native_atpg(
            cfg,
            netlist,
            _model_id(),
            campaign_id=campaign_id_for_cfg(cfg, netlist),
            max_rounds=1,
        )

    assert seen
    assert seen[0][1:] == (10, 77)
    assert not _ceiling_warnings(caplog)


def test_the_parallel_worker_reports_its_solve_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultflow.runner import parallel_solve
    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")

    def slow_timeout(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        time.sleep(0.05)
        return {"result": "TIMEOUT"}

    monkeypatch.setattr(core, "solve_fault_atpg", slow_timeout)
    fault_id, result, solved = parallel_solve.solve_fault_worker(
        ("native_stuck_at", "x.json", "c.json", "d.db", 7, [], 10, 77)
        + ("fail", True, [], [], [], False)
    )
    assert (fault_id, result) == (7, "TIMEOUT")
    assert 0.05 <= float(solved["solve_seconds"]) < 5.0
