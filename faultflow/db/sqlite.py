from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from faultflow.db.campaign import EXPECTED_USER_VERSION, SchemaError, require_v3_schema

SCHEMA_V3 = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_type TEXT NOT NULL CHECK (campaign_type IN ('comb', 'scan')),
    top TEXT NOT NULL,
    netlist_hash TEXT NOT NULL,
    cell_lib_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    yosys_version TEXT NOT NULL,
    faultflow_version TEXT NOT NULL,
    collapsing INTEGER NOT NULL,
    unsupported_cells TEXT NOT NULL,
    include_clock_faults INTEGER NOT NULL,
    include_reset_faults INTEGER NOT NULL,
    redundancy_model_id TEXT NOT NULL DEFAULT '',
    manifest_hash TEXT NOT NULL DEFAULT '',
    atpg_view_schema_ver TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL,
    vector_source TEXT,
    vector_count INTEGER NOT NULL DEFAULT 0,
    initial_ff_state TEXT NOT NULL DEFAULT 'all_zero',
    atpg_generation_seconds REAL NOT NULL DEFAULT 0.0,
    fault_simulation_seconds REAL NOT NULL DEFAULT 0.0,
    total_sim_seconds REAL NOT NULL DEFAULT 0.0,
    coverage REAL,
    atpg_terminal_reason TEXT,
    atpg_rounds INTEGER NOT NULL DEFAULT 0,
    atpg_sat INTEGER NOT NULL DEFAULT 0,
    atpg_unsat INTEGER NOT NULL DEFAULT 0,
    atpg_timeout INTEGER NOT NULL DEFAULT 0,
    atpg_unknown INTEGER NOT NULL DEFAULT 0,
    atpg_rejected_candidates INTEGER NOT NULL DEFAULT 0,
    atpg_generated_vectors INTEGER NOT NULL DEFAULT 0,
    atpg_accepted_vectors INTEGER NOT NULL DEFAULT 0,
    protocol_no_progress_rounds INTEGER NOT NULL DEFAULT 0,
    candidates_aborted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    vector_index INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    inputs TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    UNIQUE (campaign_id, id),
    UNIQUE (campaign_id, run_id, vector_index),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    fault_site_key TEXT NOT NULL,
    net_id INTEGER NOT NULL,
    net_name TEXT NOT NULL,
    node_id INTEGER NOT NULL DEFAULT -1,
    compiled_net_index INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT '',
    fault_type TEXT NOT NULL,
    status TEXT NOT NULL,
    excluded TEXT NOT NULL DEFAULT 'none',
    exclusion TEXT NOT NULL DEFAULT 'none',
    collapsed_to INTEGER,
    collapsed_into INTEGER,
    detected_by_vector INTEGER,
    redundancy_model_id TEXT,
    protocol_unresolved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (campaign_id, fault_site_key, fault_type),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS fault_detections (
    fault_id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    vector_index INTEGER NOT NULL,
    obs_net INTEGER,
    PRIMARY KEY (fault_id, run_id, vector_index),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (fault_id) REFERENCES faults(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS node_coverage (
    campaign_id INTEGER NOT NULL,
    net_id INTEGER NOT NULL,
    total INTEGER NOT NULL,
    detected INTEGER NOT NULL,
    coverage REAL NOT NULL,
    PRIMARY KEY (campaign_id, net_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS atpg_candidates (
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    sat_target_fault_id INTEGER,
    accepted_vector_id INTEGER,
    PRIMARY KEY (campaign_id, run_id, candidate_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (campaign_id, accepted_vector_id)
        REFERENCES vectors(campaign_id, id)
);

CREATE TABLE IF NOT EXISTS candidate_rejections (
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    PRIMARY KEY (campaign_id, run_id, candidate_id, fault_id),
    FOREIGN KEY (campaign_id, run_id, candidate_id)
        REFERENCES atpg_candidates(campaign_id, run_id, candidate_id),
    FOREIGN KEY (fault_id) REFERENCES faults(id)
);

CREATE TABLE IF NOT EXISTS blocked_patterns (
    campaign_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    PRIMARY KEY (campaign_id, fault_id, pattern),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (fault_id) REFERENCES faults(id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "faults"):
        return False
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < EXPECTED_USER_VERSION:
        return True
    if _table_exists(conn, "tier_b_rejections"):
        return True
    return not _table_exists(conn, "campaigns")


def init_schema(conn: sqlite3.Connection) -> None:
    if _is_legacy_schema(conn):
        raise SchemaError(
            "Legacy database schema detected. Re-run with --clean to recreate "
            "faultflow.sqlite."
        )
    conn.executescript(SCHEMA_V3)
    conn.execute(f"PRAGMA user_version = {EXPECTED_USER_VERSION}")
    conn.commit()


def summary(conn: sqlite3.Connection, campaign_id: int | None = None) -> dict[str, Any]:
    init_schema(conn)
    require_v3_schema(conn)
    if campaign_id is None:
        row = conn.execute(
            "SELECT id FROM campaigns ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {
                "total_raw_faults": 0,
                "structural_eligible": 0,
                "denominator": 0,
                "detected": 0,
                "undetected": 0,
                "redundant": 0,
                "collapsed": 0,
                "excluded_blackbox": 0,
                "excluded_clock": 0,
                "excluded_reset": 0,
                "protocol_unresolved": 0,
                "coverage_percent": None,
                "fault_coverage_percent": None,
                "test_coverage_percent": None,
            }
        campaign_id = int(row["id"])

    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_raw_faults,
          SUM(CASE WHEN exclusion = 'none' AND collapsed_into IS NULL
                    THEN 1 ELSE 0 END) AS structural_eligible,
          SUM(CASE WHEN exclusion = 'none' AND collapsed_into IS NULL
                    AND status != 'redundant' THEN 1 ELSE 0 END) AS denominator,
          SUM(CASE WHEN status = 'detected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS detected,
          SUM(CASE WHEN status = 'undetected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS undetected,
          SUM(CASE WHEN status = 'redundant' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS redundant,
          SUM(CASE WHEN collapsed_into IS NOT NULL THEN 1 ELSE 0 END) AS collapsed,
          SUM(CASE WHEN exclusion = 'blackbox' THEN 1 ELSE 0 END) AS excluded_blackbox,
          SUM(CASE WHEN exclusion = 'clock' THEN 1 ELSE 0 END) AS excluded_clock,
          SUM(CASE WHEN exclusion = 'reset' THEN 1 ELSE 0 END) AS excluded_reset,
          SUM(CASE WHEN protocol_unresolved = 1 AND exclusion = 'none'
                    AND collapsed_into IS NULL AND status != 'detected'
                    THEN 1 ELSE 0 END) AS protocol_unresolved
        FROM faults
        WHERE campaign_id = ?
        """,
        (campaign_id,),
    ).fetchone()
    data: dict[str, Any] = {key: int(row[key] or 0) for key in row.keys()}
    structural = data["structural_eligible"]
    denom = data["denominator"]
    detected = data["detected"]
    data["fault_coverage_percent"] = (
        100.0 * detected / structural if structural else None
    )
    data["test_coverage_percent"] = 100.0 * detected / denom if denom else None
    data["coverage_percent"] = data["test_coverage_percent"]
    return data
