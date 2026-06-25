from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from campaign_fixtures import campaign_id_for_cfg
from faultflow.config import ConfigError, parse_timeout_schedule
from faultflow.runner.progressive_atpg import (
    redundancy_model_id,
    run_progressive_native_atpg,
)

# Positional index of the per-fault timeout argument in core.solve_fault_atpg:
# (json_path, cell_map_path, db_path, fault_id, blocked, conflict_limit, TIMEOUT, ...)
_TIMEOUT_ARG = 6
_FAULT_ID_ARG = 3


# --------------------------------------------------------------------------- #
# parse_timeout_schedule — pure function
# --------------------------------------------------------------------------- #


def test_empty_schedule_falls_back_to_single_tier() -> None:
    # An empty schedule must reproduce the original fixed-timeout behaviour
    # exactly: one tier equal to sat_timeout_seconds.
    assert parse_timeout_schedule("", 10) == [10]
    assert parse_timeout_schedule("   ", 7) == [7]


def test_schedule_parses_comma_list_in_order() -> None:
    assert parse_timeout_schedule("2,10,60", 10) == [2, 10, 60]
    assert parse_timeout_schedule(" 2 , 10 , 60 ", 10) == [2, 10, 60]
    assert parse_timeout_schedule("5", 10) == [5]


def test_schedule_rejects_non_integer() -> None:
    with pytest.raises(ConfigError, match="must be integers"):
        parse_timeout_schedule("2,fast,60", 10)


def test_schedule_rejects_non_positive() -> None:
    with pytest.raises(ConfigError, match=">= 1 second"):
        parse_timeout_schedule("2,0,60", 10)
    with pytest.raises(ConfigError, match=">= 1 second"):
        parse_timeout_schedule("-3", 10)


# --------------------------------------------------------------------------- #
# Behavioural escalation over the native progressive loop
# --------------------------------------------------------------------------- #


def _cfg_with_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schedule: str):
    root = Path(__file__).resolve().parents[3]
    fixture = root / "tests/cpp/fixtures/tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {fixture}
cell_lib = {root / "cells/sky130/sky130_fd_sc_hd.json"}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 0
sat_conflict_limit = 100000
max_rounds = 20
sat_timeout_seconds = 7
sat_timeout_schedule = {schedule}

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from faultflow.config import load_config

    cfg = load_config(cfg_path, top="tiny_inv")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, cfg.output_dir / "tiny_inv.json")
    return cfg, cfg.output_dir / "tiny_inv.json"


def _model_id() -> str:
    return redundancy_model_id(
        {
            "netlist_hash": "x",
            "cell_lib_hash": "y",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        }
    )


def _record_timeouts(monkeypatch: pytest.MonkeyPatch):
    """Patch the solver to always time out and record (fault_id, timeout)."""
    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")

    seen: list[tuple[int, int]] = []

    def timeout_solve(*args: Any, **kwargs: Any) -> dict[str, object]:
        del kwargs
        seen.append((int(args[_FAULT_ID_ARG]), int(args[_TIMEOUT_ARG])))
        return {"result": "TIMEOUT", "vector": {}}

    monkeypatch.setattr(core, "solve_fault_atpg", timeout_solve)
    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    return seen


@pytest.mark.unit
def test_empty_schedule_uses_fixed_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _cfg_with_schedule(tmp_path, monkeypatch, "")
    seen = _record_timeouts(monkeypatch)

    campaign_id = campaign_id_for_cfg(cfg, netlist)
    run_progressive_native_atpg(
        cfg, netlist, _model_id(), campaign_id=campaign_id, max_rounds=3
    )

    assert seen, "solver was never called"
    # With no schedule every attempt uses the single fixed sat_timeout_seconds.
    assert {timeout for _, timeout in seen} == {7}


@pytest.mark.unit
def test_timeout_escalates_across_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _cfg_with_schedule(tmp_path, monkeypatch, "2,5,9")
    seen = _record_timeouts(monkeypatch)

    campaign_id = campaign_id_for_cfg(cfg, netlist)
    run_progressive_native_atpg(
        cfg, netlist, _model_id(), campaign_id=campaign_id, max_rounds=3
    )

    # A fault that keeps timing out should be retried at the next tier each round.
    per_fault: dict[int, list[int]] = {}
    for fault_id, timeout in seen:
        per_fault.setdefault(fault_id, []).append(timeout)

    escalating = [seq for seq in per_fault.values() if len(seq) >= 3]
    assert escalating, "no fault was attempted in all three rounds"
    for seq in escalating:
        # Smallest first, then escalate only after a timeout; capped at the last
        # tier so a fourth attempt (if any) would stay at 9.
        assert seq[:3] == [2, 5, 9]
        assert all(timeout == 9 for timeout in seq[3:])


@pytest.mark.unit
def test_schedule_longer_than_rounds_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, netlist = _cfg_with_schedule(tmp_path, monkeypatch, "1,2,3,4,5")
    _record_timeouts(monkeypatch)

    campaign_id = campaign_id_for_cfg(cfg, netlist)
    with caplog.at_level("WARNING"):
        run_progressive_native_atpg(
            cfg, netlist, _model_id(), campaign_id=campaign_id, max_rounds=2
        )

    assert any(
        "sat_timeout_schedule has 5 tiers" in record.message
        for record in caplog.records
    )
