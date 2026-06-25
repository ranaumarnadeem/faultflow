from __future__ import annotations

import sqlite3
from typing import Any

CAMPAIGN_TYPE_COMB = "comb"
CAMPAIGN_TYPE_SCAN = "scan"
CAMPAIGN_TYPE_SCAN_EXTEST = "scan_extest"
EXPECTED_USER_VERSION = 6


class SchemaError(RuntimeError):
    """Raised when a legacy database schema is detected."""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def require_v3_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if not _table_exists(conn, "campaigns") or version < EXPECTED_USER_VERSION:
        raise SchemaError(
            "Legacy database schema detected. Re-run with --clean to recreate "
            "faultflow.sqlite."
        )


def latest_campaign_id(conn: sqlite3.Connection, campaign_type: str) -> int | None:
    require_v3_schema(conn)
    row = conn.execute(
        """
        SELECT id FROM campaigns
        WHERE campaign_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (campaign_type,),
    ).fetchone()
    return int(row["id"]) if row else None


def campaign_fingerprint_row(
    conn: sqlite3.Connection, campaign_id: int
) -> sqlite3.Row | None:
    require_v3_schema(conn)
    return conn.execute(
        "SELECT * FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()


def fingerprint_fields(fp: dict[str, Any]) -> dict[str, Any]:
    return {
        "top": str(fp["top"]),
        "netlist_hash": str(fp["netlist_hash"]),
        "cell_lib_hash": str(fp["cell_lib_hash"]),
        "config_hash": str(fp["config_hash"]),
        "template_hash": str(fp["template_hash"]),
        "yosys_version": str(fp["yosys_version"]),
        "faultflow_version": str(fp["faultflow_version"]),
        "collapsing": int(fp["collapsing"]),
        "unsupported_cells": str(fp["unsupported_cells"]),
        "include_clock_faults": int(fp["include_clock_faults"]),
        "include_reset_faults": int(fp["include_reset_faults"]),
        "redundancy_model_id": str(fp.get("redundancy_model_id", "")),
        "manifest_hash": str(fp.get("manifest_hash", "")),
        "atpg_view_schema_ver": str(fp.get("atpg_view_schema_ver", "")),
        "fault_model": str(fp.get("fault_model", "stuck_at")),
    }


def fingerprint_mismatch_field(stored: sqlite3.Row, fp: dict[str, Any]) -> str | None:
    fields = fingerprint_fields(fp)
    for key, value in fields.items():
        if key in {"manifest_hash", "atpg_view_schema_ver"}:
            continue
        if str(stored[key]) != str(value):
            return key
    if (
        fields["manifest_hash"]
        and str(stored["manifest_hash"]) != fields["manifest_hash"]
    ):
        return "manifest_hash"
    if (
        fields["atpg_view_schema_ver"]
        and str(stored["atpg_view_schema_ver"]) != fields["atpg_view_schema_ver"]
    ):
        return "atpg_view_schema_ver"
    return None


def ensure_campaign(
    conn: sqlite3.Connection,
    campaign_type: str,
    fp: dict[str, Any],
) -> int:
    require_v3_schema(conn)
    fields = fingerprint_fields(fp)
    existing = latest_campaign_id(conn, campaign_type)
    if existing is not None:
        stored = campaign_fingerprint_row(conn, existing)
        if stored is None:
            raise SchemaError("campaign row missing")
        mismatch = fingerprint_mismatch_field(stored, fp)
        if mismatch is not None:
            raise SchemaError(
                f"Fingerprint field '{mismatch}' changed. Re-run with --clean."
            )
        return existing

    conn.execute(
        """
        INSERT INTO campaigns (
          campaign_type, top, netlist_hash, cell_lib_hash, config_hash,
          template_hash, yosys_version, faultflow_version, collapsing,
          unsupported_cells, include_clock_faults, include_reset_faults,
          redundancy_model_id, manifest_hash, atpg_view_schema_ver, fault_model
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_type,
            fields["top"],
            fields["netlist_hash"],
            fields["cell_lib_hash"],
            fields["config_hash"],
            fields["template_hash"],
            fields["yosys_version"],
            fields["faultflow_version"],
            fields["collapsing"],
            fields["unsupported_cells"],
            fields["include_clock_faults"],
            fields["include_reset_faults"],
            fields["redundancy_model_id"],
            fields["manifest_hash"],
            fields["atpg_view_schema_ver"],
            fields["fault_model"],
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def abort_pending_candidates(conn: sqlite3.Connection, campaign_id: int) -> int:
    require_v3_schema(conn)
    cur = conn.execute(
        """
        UPDATE atpg_candidates
        SET status = 'aborted'
        WHERE campaign_id = ? AND status = 'pending'
        """,
        (campaign_id,),
    )
    conn.commit()
    return int(cur.rowcount)
