from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from faultflow.db import connect, init_schema
from faultflow.reporter.unified import write_unified_report


def test_shell_file_executes_native_tcl(tmp_path: Path) -> None:
    script = tmp_path / "flow.tcl"
    script.write_text(
        "if {[expr {2 + 3}] != 5} {error bad_math}\n",
        encoding="utf-8",
    )

    assert main(["shell", "-f", str(script), "--out", str(tmp_path / "out")]) == 0


def test_shell_file_uncaught_error_exits_two(tmp_path: Path) -> None:
    script = tmp_path / "flow.tcl"
    script.write_text("error deliberate\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["shell", "-f", str(script), "--out", str(tmp_path / "out")])

    assert exc.value.code == 2


def test_shell_config_requires_design_top(tmp_path: Path) -> None:
    config = tmp_path / "missing_top.ofs"
    config.write_text(
        """
[design]
netlist = demo.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json
""".strip() + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "flow.tcl"
    script.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["shell", "-f", str(script), "-c", str(config)])

    assert exc.value.code == 2


def test_unified_report_without_campaign_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("design.json").write_text(
        '{"modules":{"demo":{"ports":{},"cells":{},"netnames":{}}}}',
        encoding="utf-8",
    )
    config = tmp_path / "demo.ofs"
    config.write_text(
        f"""
[design]
netlist = {tmp_path / "design.json"}
cell_lib = {Path(__file__).resolve().parents[3] / "cells/sky130/sky130_fd_sc_hd.json"}
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(config, "demo")

    result = write_unified_report(cfg)

    assert result.exists()
    text = result.read_text(encoding="utf-8")
    assert "campaign_state: not_run" in text
    assert "whole-design scan coverage: not_run" in text


def test_unified_report_survives_a_writer_commit_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generating the unified report during a live ATPG run must not die with
    'database is locked'. While a writer commits (EXCLUSIVE lock -- a LONG
    window on /mnt/c where each fsync is expensive), a reader opened with a raw
    sqlite3.connect has busy_timeout=0 and errors out instantly; the policy
    connect() helper (busy_timeout=30000, faultflow/db/sqlite.py) waits the
    writer out. Repro: hold BEGIN EXCLUSIVE and release it 0.3s later from a
    timer thread -- the report must wait and succeed, not crash."""
    monkeypatch.chdir(tmp_path)
    Path("design.json").write_text(
        '{"modules":{"demo":{"ports":{},"cells":{},"netnames":{}}}}',
        encoding="utf-8",
    )
    config = tmp_path / "demo.ofs"
    config.write_text(
        f"""
[design]
netlist = {tmp_path / "design.json"}
cell_lib = {Path(__file__).resolve().parents[3] / "cells/sky130/sky130_fd_sc_hd.json"}
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(config, "demo")
    cfg.ensure_workspace()
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()

    writer = sqlite3.connect(cfg.db_path, check_same_thread=False)
    release = threading.Timer(0.3, writer.rollback)
    try:
        writer.execute("BEGIN EXCLUSIVE")  # the commit window: blocks readers
        release.start()
        result = write_unified_report(cfg)
    finally:
        release.cancel()
        try:
            writer.rollback()
        except sqlite3.Error:
            pass  # already released by the timer
        writer.close()

    assert result.exists()
    assert "campaign_state: not_run" in result.read_text(encoding="utf-8")
