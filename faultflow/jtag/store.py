"""JTAG grading results, kept beside the scan campaign they credit.

Two side tables hold them, keyed to the scan campaign's own fault rows: a new scan
campaign has new fault rows (ids are never reused), so older JTAG results stop applying
without being deleted, and no fault-identity translation is needed. The scan campaign's
own summary is never changed; the report adds a ``jtag`` block (what the program
detected) and a ``combined`` block (scan and JTAG credit together).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from faultflow.jtag.grade import JtagGrade
from faultflow.jtag.program import TckProgram
from faultflow.jtag.xcheck import JtagSetup

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jtag_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    run_key TEXT NOT NULL,
    program_digest TEXT NOT NULL,
    tests TEXT NOT NULL,
    periods INTEGER NOT NULL,
    graded INTEGER NOT NULL,
    reset_path INTEGER NOT NULL,
    holds TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jtag_detections (
    run_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    test TEXT NOT NULL,
    period INTEGER NOT NULL,
    PRIMARY KEY (run_id, fault_id)
);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _tables_exist(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('jtag_runs', 'jtag_detections')"
        ).fetchone()[0]
        == 2
    )


def run_key(
    program: TckProgram, netlist_hash: str, setup: JtagSetup, blackbox: list[str]
) -> str:
    """What makes two JTAG grades the same grade: the program, the netlist, the ports
    and holds, and the blackboxes."""
    text = json.dumps(
        {
            "program": program.digest(),
            "netlist": netlist_hash,
            "ports": list(setup.ports.driven()) + [setup.ports.tdo],
            "holds": sorted(setup.holds.items()),
            "blackbox": sorted(blackbox),
        },
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def latest_run(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any] | None:
    if not _tables_exist(conn):
        return None
    row = conn.execute(
        "SELECT * FROM jtag_runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def record_run(
    conn: sqlite3.Connection,
    campaign_id: int,
    key: str,
    program: TckProgram,
    setup: JtagSetup,
    grade: JtagGrade,
) -> int:
    """Store one JTAG grade and its detections in one transaction."""
    ensure_tables(conn)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO jtag_runs(campaign_id, run_key, program_digest, tests, periods,
                                  graded, reset_path, holds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                key,
                program.digest(),
                json.dumps([list(t) for t in program.tests]),
                len(program),
                grade.graded,
                len(grade.reset_path),
                json.dumps(sorted(setup.holds.items())),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO jtag_detections(run_id, fault_id, test, period) "
            "VALUES (?, ?, ?, ?)",
            [
                (run_id, fault_id, test, period)
                for fault_id, (test, period) in sorted(grade.detected.items())
            ],
        )
    return run_id


def detected_fault_ids(conn: sqlite3.Connection, run_id: int) -> frozenset[int]:
    return frozenset(
        int(row[0])
        for row in conn.execute(
            "SELECT fault_id FROM jtag_detections WHERE run_id = ?", (run_id,)
        )
    )


def report_blocks(
    conn: sqlite3.Connection, campaign_id: int, summary: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The report's ``jtag`` and ``combined`` blocks for the campaign's latest JTAG
    grade, or None if it has none. ``summary`` is the campaign's own db.summary."""
    run = latest_run(conn, campaign_id)
    if run is None:
        return None
    run_id = int(run["id"])
    # Scan's own faults, and those it leaves to JTAG ([scan] nonscan_cells).
    eligible = "f.exclusion IN ('none', 'jtag') AND f.collapsed_into IS NULL"
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN {eligible} AND j.fault_id IS NOT NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN {eligible} AND (f.status = 'detected'
                    OR j.fault_id IS NOT NULL) THEN 1 ELSE 0 END),
          SUM(CASE WHEN {eligible} AND f.status = 'redundant'
                    AND j.fault_id IS NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN {eligible} AND f.status NOT IN ('detected', 'redundant')
                    AND j.fault_id IS NOT NULL THEN 1 ELSE 0 END)
        FROM faults f
        LEFT JOIN jtag_detections j ON j.fault_id = f.id AND j.run_id = ?
        WHERE f.campaign_id = ?
        """,
        (run_id, campaign_id),
    ).fetchone()
    jtag_detected, detected, redundant, only_jtag = (int(v or 0) for v in row)
    conflicts = [
        str(r[0])
        for r in conn.execute(
            f"""
            SELECT f.fault_site_key || ':' || f.fault_type
            FROM faults f
            JOIN jtag_detections j ON j.fault_id = f.id AND j.run_id = ?
            WHERE f.campaign_id = ? AND {eligible} AND f.status = 'redundant'
            ORDER BY f.id
            """,
            (run_id, campaign_id),
        )
    ]
    by_test = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            f"""
            SELECT j.test, COUNT(*)
            FROM jtag_detections j JOIN faults f ON f.id = j.fault_id
            WHERE j.run_id = ? AND f.campaign_id = ? AND {eligible}
            GROUP BY j.test
            """,
            (run_id, campaign_id),
        )
    }
    tests = [str(t[0]) for t in json.loads(str(run["tests"]))]
    jtag = {
        "program_digest": str(run["program_digest"]),
        "tck_periods": int(run["periods"]),
        "tests": tests,
        "graded": int(run["graded"]),
        "reset_path_ungraded": int(run["reset_path"]),
        "detected": jtag_detected,
        "detected_by_test": {t: by_test.get(t, 0) for t in tests},
        "detected_only_by_jtag": only_jtag,
        "scan_redundant_conflicts": conflicts,
        "holds": {str(k): int(v) for k, v in json.loads(str(run["holds"]))},
    }
    left_to_jtag = int(summary.get("excluded_jtag", 0))
    structural = int(summary["structural_eligible"]) + left_to_jtag
    denominator = structural - redundant
    combined = {
        "structural_eligible": structural,
        "denominator": denominator,
        "detected": detected,
        "redundant": redundant,
        "fault_coverage_percent": 100.0 * detected / structural if structural else None,
        "test_coverage_percent": (
            100.0 * detected / denominator if denominator else None
        ),
    }
    # Scan and JTAG credit only move faults between detected, undetected and
    # redundant, and bring back the ones scan left to JTAG: the Policy-3
    # invariant holds with the combined numbers too.
    moved = (
        int(summary["denominator"]) + int(summary.get("redundant", 0)) + left_to_jtag
    )
    if denominator + redundant != moved:
        raise RuntimeError(
            "combined coverage invariant failed: denominator + redundant "
            f"{denominator} + {redundant} != {moved}"
        )
    return jtag, combined
