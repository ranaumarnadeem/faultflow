"""TDD tests for faultflow/testpoint/ (M2 — add_tp / reject_tp)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

# ---------------------------------------------------------------------------
# Version stack (versions.py)
# ---------------------------------------------------------------------------


def test_version_stack_starts_empty(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    stack = TestpointVersionStack.load_or_create(tmp_path / "state.json")
    assert stack.current is None
    assert stack.baseline is None
    assert stack.all() == []


def test_version_stack_push_creates_first_entry(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    state_path = tmp_path / "state.json"
    stack = TestpointVersionStack.load_or_create(state_path)
    v = stack.push(tmp_path / "design.json", campaign_id=1, label="baseline")
    assert v.iter == 0
    assert v.campaign_id == 1
    assert v.label == "baseline"
    assert stack.current == v
    assert stack.baseline == v


def test_version_stack_push_increments_iter(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    stack = TestpointVersionStack.load_or_create(tmp_path / "state.json")
    stack.push(tmp_path / "design.json", campaign_id=1, label="baseline")
    v2 = stack.push(tmp_path / "design_tp.json", campaign_id=2, label="iter1")
    assert v2.iter == 1
    assert stack.current == v2
    assert stack.baseline is not None
    assert stack.baseline.iter == 0


def test_version_stack_pop_restores_previous(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    stack = TestpointVersionStack.load_or_create(tmp_path / "state.json")
    v0 = stack.push(tmp_path / "design.json", campaign_id=1, label="baseline")
    stack.push(tmp_path / "design_tp.json", campaign_id=2, label="iter1")
    popped = stack.pop()
    assert popped.iter == 1
    assert stack.current == v0


def test_version_stack_pop_baseline_raises(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    stack = TestpointVersionStack.load_or_create(tmp_path / "state.json")
    stack.push(tmp_path / "design.json", campaign_id=1, label="baseline")
    with pytest.raises(ValueError, match="baseline"):
        stack.pop()


def test_version_stack_persists_to_disk(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    state_path = tmp_path / "state.json"
    stack = TestpointVersionStack.load_or_create(state_path)
    stack.push(tmp_path / "design.json", campaign_id=5, label="persisted")

    stack2 = TestpointVersionStack.load_or_create(state_path)
    assert stack2.current is not None
    assert stack2.current.campaign_id == 5
    assert stack2.current.label == "persisted"


def test_version_stack_all_returns_in_order(tmp_path: Path) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    stack = TestpointVersionStack.load_or_create(tmp_path / "state.json")
    stack.push(tmp_path / "a.json", campaign_id=1)
    stack.push(tmp_path / "b.json", campaign_id=2)
    stack.push(tmp_path / "c.json", campaign_id=3)
    versions = stack.all()
    assert [v.campaign_id for v in versions] == [1, 2, 3]


# ---------------------------------------------------------------------------
# compare.py — build_comparison
# ---------------------------------------------------------------------------


def _make_summary(
    campaign_id: int,
    detected: int,
    denominator: int,
    fc_pct: float,
    tc_pct: float,
    vector_count: int = 10,
    atpg_s: float = 1.0,
    sim_s: float = 2.0,
    total_s: float = 3.0,
    rounds: int = 2,
) -> Any:
    from faultflow.testpoint.compare import CampaignSummary

    return CampaignSummary(
        campaign_id=campaign_id,
        detected=detected,
        denominator=denominator,
        undetected=denominator - detected,
        fault_coverage_percent=fc_pct,
        test_coverage_percent=tc_pct,
        vector_count=vector_count,
        atpg_seconds=atpg_s,
        sim_seconds=sim_s,
        total_seconds=total_s,
        atpg_rounds=rounds,
    )


def test_build_comparison_delta_coverage(tmp_path: Path) -> None:
    from faultflow.testpoint.compare import build_comparison

    baseline = _make_summary(1, detected=80, denominator=100, fc_pct=80.0, tc_pct=75.0)
    tp = _make_summary(2, detected=90, denominator=110, fc_pct=81.818, tc_pct=77.0)
    result = build_comparison(baseline, tp, rescued=8)

    assert result["fault_coverage_pct"]["baseline"] == 80.0
    assert result["fault_coverage_pct"]["tp"] == pytest.approx(81.818, abs=0.01)
    assert result["fault_coverage_pct"]["delta"] == pytest.approx(1.818, abs=0.01)


def test_build_comparison_detected_counts(tmp_path: Path) -> None:
    from faultflow.testpoint.compare import build_comparison

    baseline = _make_summary(1, 80, 100, 80.0, 75.0)
    tp = _make_summary(2, 90, 110, 81.8, 77.0)
    result = build_comparison(baseline, tp, rescued=8)

    assert result["detected"]["baseline"] == 80
    assert result["detected"]["tp"] == 90
    assert result["detected"]["delta"] == 10


def test_build_comparison_original_rescued(tmp_path: Path) -> None:
    from faultflow.testpoint.compare import build_comparison

    baseline = _make_summary(1, 80, 100, 80.0, 75.0)
    tp = _make_summary(2, 90, 110, 81.8, 77.0)
    result = build_comparison(baseline, tp, rescued=8)

    assert result["original_rescued"] == 8


def test_build_comparison_tp_inserted_from_report(tmp_path: Path) -> None:
    from faultflow.testpoint.compare import build_comparison

    baseline = _make_summary(1, 80, 100, 80.0, 75.0)
    tp = _make_summary(2, 90, 110, 81.8, 77.0)
    tp_report = {"total": 5, "obs": 3, "ctrl": 2}
    result = build_comparison(baseline, tp, rescued=8, tp_report=tp_report)

    assert result["tp_inserted"]["total"] == 5
    assert result["tp_inserted"]["obs"] == 3
    assert result["tp_inserted"]["ctrl"] == 2


def test_build_comparison_no_tp_report_zeros(tmp_path: Path) -> None:
    from faultflow.testpoint.compare import build_comparison

    baseline = _make_summary(1, 80, 100, 80.0, 75.0)
    tp = _make_summary(2, 90, 110, 81.8, 77.0)
    result = build_comparison(baseline, tp, rescued=0)

    assert result["tp_inserted"]["total"] == 0


# ---------------------------------------------------------------------------
# compare.py — compute_rescued (SQL logic)
# ---------------------------------------------------------------------------


def _seed_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE faults (
            id INTEGER PRIMARY KEY,
            campaign_id INTEGER,
            net_name TEXT,
            fault_type TEXT,
            status TEXT,
            collapsed_into INTEGER,
            exclusion TEXT DEFAULT 'none'
        );
        """)
    rows: list[tuple[Any, ...]] = [
        # campaign 1 (baseline): net_a undetected, net_b detected, tp_obs_0 undetected
        (1, 1, "net_a", "SA0", "undetected", None, "none"),
        (2, 1, "net_b", "SA1", "detected", None, "none"),
        (3, 1, "tp_obs_0", "SA0", "undetected", None, "none"),
        # campaign 2 (TP): net_a SA0 now detected, tp_obs_0 detected
        (4, 2, "net_a", "SA0", "detected", None, "none"),
        (5, 2, "net_b", "SA1", "detected", None, "none"),
        (6, 2, "tp_obs_0", "SA0", "detected", None, "none"),
    ]
    conn.executemany(
        "INSERT INTO faults VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_compute_rescued_counts_only_original_nets() -> None:
    from faultflow.testpoint.compare import compute_rescued

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_db(conn)
    rescued = compute_rescued(conn, baseline_id=1, tp_id=2)
    # net_a rescued; tp_obs_0 excluded even though detected in tp
    assert rescued == 1


def test_compute_rescued_zero_when_none() -> None:
    from faultflow.testpoint.compare import compute_rescued

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_db(conn)
    # flip TP back — net_a still undetected
    conn.execute(
        "UPDATE faults SET status='undetected' WHERE campaign_id=2 AND net_name='net_a'"
    )
    conn.commit()
    rescued = compute_rescued(conn, baseline_id=1, tp_id=2)
    assert rescued == 0


# ---------------------------------------------------------------------------
# opentest_driver.py — unit tests (no real OT binary)
# ---------------------------------------------------------------------------


def test_insert_test_points_raises_if_binary_missing(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    with pytest.raises(FileNotFoundError, match="opentest"):
        insert_test_points(
            "/nonexistent/opentest",
            input_netlist=tmp_path / "in.json",
            output_netlist=tmp_path / "out.json",
        )


def test_insert_test_points_raises_on_nonzero_exit(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    (tmp_path / "in.json").write_text("{}")

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stderr = "error from OT"
    fake_proc.stdout = ""

    with patch("shutil.which", return_value="/usr/bin/opentest"), patch(
        "subprocess.run", return_value=fake_proc
    ):
        with pytest.raises(RuntimeError, match="opentest failed"):
            insert_test_points(
                "opentest",
                input_netlist=tmp_path / "in.json",
                output_netlist=tmp_path / "out.json",
            )


def test_insert_test_points_parses_stdout_json(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    (tmp_path / "in.json").write_text("{}")
    (tmp_path / "out.json").write_text("{}")

    report = {"obs_count": 3, "ctrl_count": 2}
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = json.dumps(report)
    fake_proc.stderr = ""

    with patch("shutil.which", return_value="/usr/bin/opentest"), patch(
        "subprocess.run", return_value=fake_proc
    ):
        result = insert_test_points(
            "opentest",
            input_netlist=tmp_path / "in.json",
            output_netlist=tmp_path / "out.json",
        )
    assert result.obs_count == 3
    assert result.ctrl_count == 2
    assert result.total_count == 5
    assert result.output_netlist == tmp_path / "out.json"


def test_insert_test_points_falls_back_to_report_file(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    (tmp_path / "in.json").write_text("{}")
    (tmp_path / "out.json").write_text("{}")
    report_file = tmp_path / "out.tpi_report.json"
    report_file.write_text(json.dumps({"obs_count": 1, "ctrl_count": 4}))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "not json"
    fake_proc.stderr = ""

    with patch("shutil.which", return_value="/usr/bin/opentest"), patch(
        "subprocess.run", return_value=fake_proc
    ):
        result = insert_test_points(
            "opentest",
            input_netlist=tmp_path / "in.json",
            output_netlist=tmp_path / "out.json",
        )
    assert result.obs_count == 1
    assert result.ctrl_count == 4


def test_insert_test_points_default_tech_is_sky130(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    (tmp_path / "in.json").write_text("{}")
    (tmp_path / "out.json").write_text("{}")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "{}"
    fake_proc.stderr = ""
    captured: list[list[str]] = []

    def _capture(cmd: list[str], **_kw: object) -> object:
        captured.append(cmd)
        return fake_proc

    with patch("shutil.which", return_value="/usr/bin/opentest"), patch(
        "subprocess.run", side_effect=_capture
    ):
        insert_test_points(
            "opentest",
            input_netlist=tmp_path / "in.json",
            output_netlist=tmp_path / "out.json",
        )
    assert captured, "subprocess.run not called"
    cmd = captured[0]
    assert "--tech" in cmd
    idx = cmd.index("--tech")
    assert cmd[idx + 1] == "sky130"


def test_insert_test_points_explicit_tech_passed(tmp_path: Path) -> None:
    from faultflow.testpoint.opentest_driver import insert_test_points

    (tmp_path / "in.json").write_text("{}")
    (tmp_path / "out.json").write_text("{}")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "{}"
    fake_proc.stderr = ""
    captured: list[list[str]] = []

    def _capture(cmd: list[str], **_kw: object) -> object:
        captured.append(cmd)
        return fake_proc

    with patch("shutil.which", return_value="/usr/bin/opentest"), patch(
        "subprocess.run", side_effect=_capture
    ):
        insert_test_points(
            "opentest",
            input_netlist=tmp_path / "in.json",
            output_netlist=tmp_path / "out.json",
            tech="osu035",
        )
    cmd = captured[0]
    idx = cmd.index("--tech")
    assert cmd[idx + 1] == "osu035"


# ---------------------------------------------------------------------------
# help_text — add_tp / reject_tp registered
# ---------------------------------------------------------------------------


def test_help_text_add_tp_registered() -> None:
    from faultflow.shell.help_text import COMMAND_HELP

    assert "add_tp" in COMMAND_HELP
    entry = COMMAND_HELP["add_tp"]
    assert entry.category == "Test Point"
    assert "OpenTestability" in entry.details


def test_help_text_reject_tp_registered() -> None:
    from faultflow.shell.help_text import COMMAND_HELP

    assert "reject_tp" in COMMAND_HELP
    entry = COMMAND_HELP["reject_tp"]
    assert entry.category == "Test Point"
    assert "version stack" in entry.details.lower() or "stack" in entry.details.lower()


def test_help_overview_includes_test_point_section() -> None:
    from faultflow.shell.help_text import render_help_overview

    overview = render_help_overview()
    assert "Test Point" in overview
    assert "add_tp" in overview
    assert "reject_tp" in overview


# ---------------------------------------------------------------------------
# tcl_bridge._add_tp / _reject_tp flag parsing
# ---------------------------------------------------------------------------


def _tiny_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "modules": {
                    "demo": {
                        "attributes": {"top": "1"},
                        "ports": {},
                        "cells": {},
                        "netnames": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _loaded_session(tmp_path: Path) -> ProjectSession:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


def test_tcl_add_tp_parses_all_flags_into_session_kwargs(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    captured: dict[str, object] = {}

    def _fake_add_tp(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    session.add_tp = _fake_add_tp  # type: ignore[method-assign]

    bridge.call("add_tp", "-m", "cont0", "-t", "80", "-n", "16")

    assert captured == {"metric": "cont0", "threshold": 80, "max_points": 16}


def test_tcl_add_tp_long_flags_parsed(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    captured: dict[str, object] = {}

    def _fake_add_tp(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    session.add_tp = _fake_add_tp  # type: ignore[method-assign]

    bridge.call(
        "add_tp",
        "--metric",
        "sc0",
        "--threshold",
        "50",
        "--max-points",
        "4",
    )

    assert captured == {"metric": "sc0", "threshold": 50, "max_points": 4}


def test_tcl_add_tp_no_flags_calls_with_empty_kwargs(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    captured: dict[str, object] = {}

    def _fake_add_tp(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    session.add_tp = _fake_add_tp  # type: ignore[method-assign]

    bridge.call("add_tp")

    assert captured == {}


def test_tcl_add_tp_bad_threshold_integer_raises(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as excinfo:
        bridge.call("add_tp", "-t", "not-a-number")

    assert excinfo.value.code == ("FAULTFLOW", "CONFIG", "INVALID_VALUE")


def test_tcl_add_tp_bad_max_points_integer_raises(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as excinfo:
        bridge.call("add_tp", "-n", "abc")

    assert excinfo.value.code == ("FAULTFLOW", "CONFIG", "INVALID_VALUE")


def test_tcl_add_tp_missing_flag_value_raises(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as excinfo:
        bridge.call("add_tp", "-m")

    assert excinfo.value.code == ("FAULTFLOW", "CONFIG", "INVALID_OPTION")


def test_tcl_add_tp_unknown_flag_raises(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as excinfo:
        bridge.call("add_tp", "--bogus", "value")

    assert excinfo.value.code == ("FAULTFLOW", "CONFIG", "INVALID_OPTION")


def test_tcl_reject_tp_calls_session_with_no_args(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    called: dict[str, bool] = {"invoked": False}

    def _fake_reject_tp() -> str:
        called["invoked"] = True
        return "rejected"

    session.reject_tp = _fake_reject_tp  # type: ignore[method-assign]

    result = bridge.call("reject_tp")

    assert called["invoked"] is True
    assert result == "rejected"


def test_tcl_reject_tp_rejects_unexpected_args(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as excinfo:
        bridge.call("reject_tp", "extra")

    assert excinfo.value.code == ("FAULTFLOW", "CONFIG", "INVALID_OPTION")


# ---------------------------------------------------------------------------
# session.add_tp — NO_BASELINE_CAMPAIGN precondition
# ---------------------------------------------------------------------------


def test_session_add_tp_no_db_raises_no_baseline_campaign(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    with pytest.raises(ShellError) as excinfo:
        session.add_tp()

    assert excinfo.value.code == ("FAULTFLOW", "PRECONDITION", "NO_BASELINE_CAMPAIGN")


def test_session_add_tp_db_without_comb_campaign_raises_no_baseline_campaign(
    tmp_path: Path,
) -> None:
    session = _loaded_session(tmp_path)
    cfg = session.materialize_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    from faultflow.db import connect, init_schema

    with connect(cfg.db_path) as conn:
        init_schema(conn)

    with pytest.raises(ShellError) as excinfo:
        session.add_tp()

    assert excinfo.value.code == ("FAULTFLOW", "PRECONDITION", "NO_BASELINE_CAMPAIGN")


# ---------------------------------------------------------------------------
# session.reject_tp — NO_TP_TO_REJECT precondition (session level)
# ---------------------------------------------------------------------------


def test_session_reject_tp_at_baseline_only_raises_no_tp_to_reject(
    tmp_path: Path,
) -> None:
    from faultflow.testpoint.versions import TestpointVersionStack

    session = _loaded_session(tmp_path)
    assert session.source is not None
    stack = TestpointVersionStack.load_or_create(session._tp_stack_path())
    stack.push(session.source, campaign_id=1, label="baseline")
    assert len(stack.all()) == 1

    with pytest.raises(ShellError) as excinfo:
        session.reject_tp()

    assert excinfo.value.code == ("FAULTFLOW", "TP", "NO_TP_TO_REJECT")


def test_session_reject_tp_with_no_stack_at_all_raises_no_tp_to_reject(
    tmp_path: Path,
) -> None:
    session = _loaded_session(tmp_path)

    with pytest.raises(ShellError) as excinfo:
        session.reject_tp()

    assert excinfo.value.code == ("FAULTFLOW", "TP", "NO_TP_TO_REJECT")
