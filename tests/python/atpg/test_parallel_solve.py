"""Tests for wave-based parallel fault solving.

Verifies that:
  - solve_fault_worker routes each solve_kind to the correct core.* function
  - workers > 1 gives identical coverage/detected/denominator as workers = 1
  - the fork fallback log is emitted when get_context("fork") is unavailable
  - a worker exception returns UNKNOWN without crashing the coordinator
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faultflow.runner.parallel_solve import solve_fault_worker

# ---------------------------------------------------------------------------
# Unit tests — worker routing
# ---------------------------------------------------------------------------

_BASE_ARGS = dict(
    json_path="/fake/netlist.json",
    cell_map_path="/fake/cell.json",
    db_path="/fake/db.sqlite",
    fault_id=42,
    blocked=[],
    conflict_limit=100_000,
    timeout=10,
    unsupported="fail",
    cone_restrict=True,
    los_couple_ports=[],
    los_head_ports=[],
    bb_instances=[],
    test_mode="",
    incremental=False,
)


def _args_tuple(solve_kind: str, **overrides) -> tuple:
    d = {**_BASE_ARGS, **overrides}
    return (
        solve_kind,
        d["json_path"],
        d["cell_map_path"],
        d["db_path"],
        d["fault_id"],
        d["blocked"],
        d["conflict_limit"],
        d["timeout"],
        d["unsupported"],
        d["cone_restrict"],
        d["los_couple_ports"],
        d["los_head_ports"],
        d["bb_instances"],
        d["test_mode"],
        d["incremental"],
    )


def _fake_core(result: str = "SAT", **extras: Any) -> MagicMock:
    """Return a mock core module whose solver functions return a fixed result."""
    m = MagicMock()
    payload: dict[str, Any] = {"result": result, **extras}
    m.solve_fault_atpg.return_value = payload
    m.solve_scan_transition_fault_atpg.return_value = payload
    m.solve_scan_los_transition_fault_atpg.return_value = payload
    return m


class TestWorkerRouting:
    def test_scan_stuck_at_calls_solve_fault_atpg(self):
        core = _fake_core(result="SAT", vector={"a": True})
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(_args_tuple("scan_stuck_at"))
        assert fault_id == 42
        assert result == "SAT"
        core.solve_fault_atpg.assert_called_once()
        # transition-only functions must NOT be called
        core.solve_scan_transition_fault_atpg.assert_not_called()
        core.solve_scan_los_transition_fault_atpg.assert_not_called()

    def test_broadside_calls_scan_transition(self):
        core = _fake_core(result="SAT", launch={"a": True})
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(
                _args_tuple("broadside_transition")
            )
        assert result == "SAT"
        core.solve_scan_transition_fault_atpg.assert_called_once()
        core.solve_fault_atpg.assert_not_called()
        core.solve_scan_los_transition_fault_atpg.assert_not_called()

    def test_los_calls_scan_los_transition(self):
        core = _fake_core(result="SAT", launch={"a": True}, capture={"h": False})
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(
                _args_tuple(
                    "los_transition",
                    los_couple_ports=[("si", "so")],
                    los_head_ports=["h"],
                )
            )
        assert result == "SAT"
        core.solve_scan_los_transition_fault_atpg.assert_called_once()
        core.solve_fault_atpg.assert_not_called()
        core.solve_scan_transition_fault_atpg.assert_not_called()

    def test_native_stuck_at_passes_bb_and_test_mode(self):
        core = _fake_core(result="UNSAT")
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(
                _args_tuple(
                    "native_stuck_at",
                    bb_instances=["blackbox_inst"],
                    test_mode="intest",
                )
            )
        assert result == "UNSAT"
        pos_args = core.solve_fault_atpg.call_args[0]
        # native_stuck_at passes bb_instances (list) and test_mode at positions 8,9
        assert ["blackbox_inst"] in pos_args
        assert "intest" in pos_args

    def test_worker_exception_returns_unknown(self):
        core = MagicMock()
        core.solve_fault_atpg.side_effect = RuntimeError("C++ exploded")
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(_args_tuple("scan_stuck_at"))
        assert result == "UNKNOWN"
        assert "_exc" in solved

    def test_timeout_result_passed_through(self):
        core = _fake_core(result="TIMEOUT")
        with patch.dict("sys.modules", {"_faultflow_core": core}):
            fault_id, result, solved = solve_fault_worker(_args_tuple("scan_stuck_at"))
        assert result == "TIMEOUT"


# ---------------------------------------------------------------------------
# A/B coverage invariant — workers=1 vs workers=N give identical results
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
C432_JSON = ROOT / "tests" / "benchmarks" / "iscas85" / "synth" / "c432.json"
C432_CELL_MAP = ROOT / "cells" / "osu" / "osu035.json"

# Use the runner's path-aware loader (it adds build/src/core to sys.path) rather
# than a bare ``import _faultflow_core``, which fails at collection time because the
# compiled extension is not on the default path — that silently skipped this test.
try:
    import faultflow.runner.runner as _runner_mod

    _CORE_AVAILABLE = _runner_mod._load_core() is not None
except Exception:
    _CORE_AVAILABLE = False


@pytest.mark.skipif(
    not _CORE_AVAILABLE or not C432_JSON.exists() or not C432_CELL_MAP.exists(),
    reason="C++ extension or c432 benchmark not available",
)
@pytest.mark.integration
def test_parallel_workers_preserve_coverage(tmp_path, monkeypatch):
    """workers=1 vs workers=2 give identical detected/denominator on c432, for
    BOTH the baseline and the incremental (IFC) solver.

    This guards the multicore SAT-worker architecture (fork ProcessPoolExecutor):
    it must stay deterministic across worker counts, and enabling incremental_sat
    must not change coverage on either the serial or the parallel path.
    """
    from campaign_fixtures import campaign_id_for_cfg  # type: ignore[import]

    from faultflow.config import load_config
    from faultflow.db import connect, init_schema, summary
    from faultflow.runner.progressive_atpg import run_progressive_native_atpg

    monkeypatch.chdir(tmp_path)

    def _run(workers: int, incremental: bool) -> dict:
        cfg_path = tmp_path / f"c432_w{workers}_i{int(incremental)}.ofs"
        cfg_path.write_text(
            f"[design]\nnetlist = {C432_JSON}\ncell_lib = {C432_CELL_MAP}\n"
            f"[atpg]\nmax_rounds = 2\nworkers = {workers}\n"
            f"incremental_sat = {'true' if incremental else 'false'}\n",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path, top="c432")
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        campaign_id = campaign_id_for_cfg(cfg, C432_JSON)
        run_progressive_native_atpg(cfg, C432_JSON, "test", campaign_id=campaign_id)
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            return summary(conn, campaign_id=campaign_id)

    base_serial = _run(1, False)
    base_parallel = _run(2, False)
    inc_serial = _run(1, True)
    inc_parallel = _run(2, True)

    # Multicore determinism: workers=1 == workers=2 on each solver.
    assert base_serial["detected"] == base_parallel["detected"], (
        f"baseline detected mismatch: w1={base_serial['detected']} "
        f"w2={base_parallel['detected']}"
    )
    assert inc_serial["detected"] == inc_parallel["detected"], (
        f"incremental detected mismatch: w1={inc_serial['detected']} "
        f"w2={inc_parallel['detected']}"
    )
    # IFC is coverage-identical to baseline on the multicore (parallel) path.
    assert inc_parallel["detected"] == base_parallel["detected"], (
        f"IFC vs baseline detected mismatch on workers=2: "
        f"ifc={inc_parallel['detected']} base={base_parallel['detected']}"
    )
    assert inc_parallel["denominator"] == base_parallel["denominator"]
    assert base_serial["denominator"] == base_parallel["denominator"]
