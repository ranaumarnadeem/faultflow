from __future__ import annotations

import sqlite3


def insert_campaign(
    conn: sqlite3.Connection,
    *,
    campaign_type: str = "comb",
    top: str = "demo",
    netlist_hash: str = "net",
    cell_lib_hash: str = "cell",
    config_hash: str = "cfg",
    template_hash: str = "tmpl",
    yosys_version: str = "yosys",
    faultflow_version: str = "pipeline-v1",
) -> int:
    conn.execute(
        """
        INSERT INTO campaigns (
          campaign_type, top, netlist_hash, cell_lib_hash, config_hash,
          template_hash, yosys_version, faultflow_version, collapsing,
          unsupported_cells, include_clock_faults, include_reset_faults
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'fail', 0, 0)
        """,
        (
            campaign_type,
            top,
            netlist_hash,
            cell_lib_hash,
            config_hash,
            template_hash,
            yosys_version,
            faultflow_version,
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def insert_run(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    status: str = "complete",
    vector_source: str = "native_sat_atpg",
    vector_count: int = 0,
    atpg_terminal_reason: str | None = None,
    atpg_rounds: int = 0,
    atpg_sat: int = 0,
    atpg_unsat: int = 0,
) -> int:
    conn.execute(
        """
        INSERT INTO runs(
          campaign_id, status, vector_source, vector_count,
          atpg_terminal_reason, atpg_rounds, atpg_sat, atpg_unsat,
          atpg_timeout, atpg_unknown
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """,
        (
            campaign_id,
            status,
            vector_source,
            vector_count,
            atpg_terminal_reason,
            atpg_rounds,
            atpg_sat,
            atpg_unsat,
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def insert_fault_row(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    net_id: int,
    net_name: str,
    compiled_net_index: int,
    fault_type: str,
    status: str,
    exclusion: str = "none",
    fault_site_key: str | None = None,
    collapsed_into: int | None = None,
) -> None:
    site_key = fault_site_key or f"net:{net_id}:stem"
    conn.execute(
        """
        INSERT INTO faults(
          campaign_id, fault_site_key, net_id, net_name, node_id,
          compiled_net_index, type, fault_type, status, excluded, exclusion,
          collapsed_to, collapsed_into
        ) VALUES (?, ?, ?, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            site_key,
            net_id,
            net_name,
            compiled_net_index,
            fault_type,
            fault_type,
            status,
            exclusion if exclusion == "none" else exclusion,
            exclusion,
            collapsed_into,
            collapsed_into,
        ),
    )
