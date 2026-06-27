"""CLI logging: --verbose must actually enable DEBUG, and the intra-round grading
heartbeat must throttle so long rounds don't spam."""

from __future__ import annotations

import logging
import time

import faultflow.shell.repl as repl
from faultflow.runner.progressive_atpg import log_grade_progress


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


def test_log_grade_progress_throttles(caplog) -> None:
    logger = logging.getLogger("test.grade")
    now = time.perf_counter()

    # A recent tick: within the interval -> no log, last-tick unchanged.
    with caplog.at_level(logging.INFO, logger="test.grade"):
        unchanged = log_grade_progress(
            logger, now, now - 1.0, 1, 5, 10, 3, interval=15.0
        )
    assert unchanged == now
    assert "grading vector" not in caplog.text

    # A stale tick: interval elapsed -> logs once, returns a fresh tick.
    with caplog.at_level(logging.INFO, logger="test.grade"):
        fresh = log_grade_progress(
            logger, now - 100.0, now - 100.0, 2, 5, 10, 3, interval=15.0
        )
    assert fresh > now - 100.0
    assert "round  2  grading vector 5/10  accepted=3" in caplog.text
