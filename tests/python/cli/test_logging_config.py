"""CLI logging: --verbose stays wired (DEBUG is intentionally empty for now), and
the always-on intra-round grading heartbeat reports the live detected count so a
long ATPG round shows real progress instead of dead air."""

from __future__ import annotations

import logging

import faultflow.shell.repl as repl
from faultflow.runner.progressive_atpg import GradeHeartbeat, SolveHeartbeat


def test_configure_logging_verbose_toggles_level() -> None:
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = root.handlers[:]
    try:
        repl.configure_logging(False)
        assert root.level == logging.INFO
        repl.configure_logging(True)
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_grade_heartbeat_reports_live_detected_delta(caplog) -> None:
    logger = logging.getLogger("test.grade")
    counts = iter([100, 175])
    # interval=0 forces every tick to emit so the delta is observable.
    hb = GradeHeartbeat(logger, 2, 10, lambda: next(counts), interval=0.0)
    with caplog.at_level(logging.INFO, logger="test.grade"):
        hb.tick(5)
        hb.tick(10)
    assert "round  2  graded 5/10 vectors  detected=100 (+0)" in caplog.text
    assert "graded 10/10 vectors  detected=175 (+75)" in caplog.text


def test_grade_heartbeat_throttles_and_skips_db_query(caplog) -> None:
    calls = {"n": 0}

    def _detected() -> int:
        calls["n"] += 1
        return 5

    logger = logging.getLogger("test.grade2")
    hb = GradeHeartbeat(logger, 1, 10, _detected, interval=1000.0)
    with caplog.at_level(logging.INFO, logger="test.grade2"):
        hb.tick(1)
    # Within the interval: no heartbeat, and the (DB-touching) detected_fn is not
    # even called.
    assert caplog.text == ""
    assert calls["n"] == 0


def test_solve_heartbeat_reports_solved_detected_and_eta(caplog) -> None:
    logger = logging.getLogger("test.solve")
    counts = iter([100, 175])
    # interval=0 forces every tick to emit so the delta is observable.
    hb = SolveHeartbeat(logger, 3, 20, lambda: next(counts), interval=0.0)
    with caplog.at_level(logging.INFO, logger="test.solve"):
        hb.tick(5)
        hb.tick(10)
    assert "round  3  solved 5/20 faults  detected=100 (+0)" in caplog.text
    assert "solved 10/20 faults  detected=175 (+75)" in caplog.text
    # ETA is timing-dependent; assert only that the field is present.
    assert "~ETA=" in caplog.text


def test_solve_heartbeat_throttles_and_skips_db_query(caplog) -> None:
    calls = {"n": 0}

    def _detected() -> int:
        calls["n"] += 1
        return 5

    logger = logging.getLogger("test.solve2")
    hb = SolveHeartbeat(logger, 1, 10, _detected, interval=1000.0)
    with caplog.at_level(logging.INFO, logger="test.solve2"):
        hb.tick(1)
    # Within the interval: no heartbeat, and the detected_fn is not called.
    assert caplog.text == ""
    assert calls["n"] == 0
