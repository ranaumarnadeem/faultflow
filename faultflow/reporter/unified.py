from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faultflow.config import FaultflowConfig
from faultflow.db import connect, latest_campaign_id, summary


def _campaign(conn: sqlite3.Connection, campaign_type: str) -> dict[str, Any] | None:
    campaign_id = latest_campaign_id(conn, campaign_type)
    if campaign_id is None:
        return None
    data = summary(conn, campaign_id=campaign_id)
    run = conn.execute(
        """
        SELECT vector_count, atpg_terminal_reason, atpg_rounds,
               atpg_generation_seconds, fault_simulation_seconds,
               total_sim_seconds
        FROM runs
        WHERE campaign_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    return {
        "id": campaign_id,
        "summary": data,
        "run": dict(run) if run is not None else {},
    }


def _campaign_lines(title: str, data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return [title, "campaign_state: not_run", ""]
    summary_data = data["summary"]
    run = data["run"]
    coverage = summary_data.get("test_coverage_percent")
    coverage_text = "n/a" if coverage is None else f"{float(coverage):.3f}%"
    return [
        title,
        f"campaign_id: {data['id']}",
        "campaign_state: available",
        f"structural_eligible: {summary_data.get('structural_eligible', 0)}",
        f"effective_denominator: {summary_data.get('denominator', 0)}",
        f"detected: {summary_data.get('detected', 0)}",
        f"redundant: {summary_data.get('redundant', 0)}",
        f"protocol_unresolved: {summary_data.get('protocol_unresolved', 0)}",
        f"fault_coverage_percent: {summary_data.get('fault_coverage_percent')}",
        f"test_coverage_percent: {coverage_text}",
        f"terminal_reason: {run.get('atpg_terminal_reason') or 'n/a'}",
        f"vectors: {run.get('vector_count', 0)}",
        f"atpg_seconds: {float(run.get('atpg_generation_seconds', 0.0)):.3f}",
        f"simulation_seconds: {float(run.get('fault_simulation_seconds', 0.0)):.3f}",
        "",
    ]


def write_unified_report(cfg: FaultflowConfig) -> Path:
    cfg.ensure_workspace()
    scan: dict[str, Any] | None = None
    comb: dict[str, Any] | None = None
    if cfg.db_path.exists():
        # Policy connect(): busy_timeout=30000 so a report generated beside a
        # live ATPG writer waits out the commit window instead of failing after
        # the stdlib's 5s default (long single commits are real on /mnt/c).
        conn = connect(cfg.db_path)
        try:
            scan = _campaign(conn, "scan")
            comb = _campaign(conn, "comb")
        finally:
            conn.close()

    manifest: dict[str, Any] | None = None
    if cfg.scan_manifest_path.exists():
        loaded = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded

    lines = [
        f"faultflow unified report for {cfg.top}",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"source: {cfg.netlist}",
        f"cell_map: {cfg.cell_lib}",
        "",
        (
            "whole-design scan coverage: not_run"
            if scan is None
            else "whole-design scan coverage: available"
        ),
        "",
    ]
    if manifest is not None:
        latest = manifest.get("latest_check")
        check = (
            latest.get("status", "not_run") if isinstance(latest, dict) else "not_run"
        )
        lines.extend(
            [
                "scan infrastructure",
                f"chains: {manifest.get('chain_count', 0)}",
                f"scan_cells: {manifest.get('cell_count', 0)}",
                f"latest_check: {check}",
                "",
            ]
        )
    lines.extend(_campaign_lines("scan campaign (headline)", scan))
    lines.extend(_campaign_lines("combinational campaign (comparison only)", comb))
    lines.append(
        "note: combinational and scan counts are not added because their "
        "fault universes overlap"
    )
    path = cfg.output_dir / "report.rpt"
    temporary = path.with_suffix(".rpt.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
