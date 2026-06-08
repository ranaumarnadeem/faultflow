from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS design_fingerprint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL,
    vector_source TEXT,
    vector_count INTEGER NOT NULL DEFAULT 0,
    coverage REAL
);

CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    vector_index INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    inputs TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, vector_index),
    FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    UNIQUE(compiled_net_index, fault_type)
);

CREATE TABLE IF NOT EXISTS fault_detections (
    fault_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    vector_index INTEGER NOT NULL,
    obs_net INTEGER,
    PRIMARY KEY(fault_id, run_id, vector_index),
    FOREIGN KEY(fault_id) REFERENCES faults(id),
    FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS node_coverage (
    net_id INTEGER PRIMARY KEY,
    total INTEGER NOT NULL,
    detected INTEGER NOT NULL,
    coverage REAL NOT NULL
);
"""


MIGRATIONS = [
    ("vectors", "inputs", "TEXT NOT NULL DEFAULT '{}'"),
    ("vectors", "expected", "TEXT NOT NULL DEFAULT '{}'"),
    ("vectors", "verified", "INTEGER NOT NULL DEFAULT 0"),
    ("faults", "net_name", "TEXT NOT NULL DEFAULT ''"),
    ("faults", "node_id", "INTEGER NOT NULL DEFAULT -1"),
    ("faults", "type", "TEXT NOT NULL DEFAULT ''"),
    ("faults", "excluded", "TEXT NOT NULL DEFAULT 'none'"),
    ("faults", "collapsed_to", "INTEGER"),
]


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, spec in MIGRATIONS:
        if not _has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    conn.commit()


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    init_schema(conn)
    row = conn.execute("""
        SELECT
          COUNT(*) AS total_raw_faults,
          SUM(CASE WHEN exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END)
            AS denominator,
          SUM(CASE WHEN status = 'detected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS detected,
          SUM(CASE WHEN status = 'undetected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS undetected,
          SUM(CASE WHEN collapsed_into IS NOT NULL THEN 1 ELSE 0 END) AS collapsed,
          SUM(CASE WHEN exclusion = 'blackbox' THEN 1 ELSE 0 END) AS excluded_blackbox,
          SUM(CASE WHEN exclusion = 'clock' THEN 1 ELSE 0 END) AS excluded_clock,
          SUM(CASE WHEN exclusion = 'reset' THEN 1 ELSE 0 END) AS excluded_reset
        FROM faults
        """).fetchone()
    data: dict[str, Any] = {key: int(row[key] or 0) for key in row.keys()}
    denom = data["denominator"]
    data["coverage_percent"] = 100.0 * data["detected"] / denom if denom else None
    return data
