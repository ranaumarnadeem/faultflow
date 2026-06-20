"""Comparison table between a baseline and a post-TPI campaign."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class CampaignSummary:
    campaign_id: int
    detected: int
    denominator: int
    undetected: int
    fault_coverage_percent: float
    test_coverage_percent: float
    vector_count: int
    atpg_seconds: float
    sim_seconds: float
    total_seconds: float
    atpg_rounds: int


def load_summary(conn: sqlite3.Connection, campaign_id: int) -> CampaignSummary:
    from faultflow.db import summary as _db_summary

    data = _db_summary(conn, campaign_id=campaign_id)
    row = conn.execute(
        """
        SELECT vector_count, atpg_generation_seconds, fault_simulation_seconds,
               total_sim_seconds, atpg_rounds
        FROM runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    run: dict[str, Any] = dict(row) if row is not None else {}
    return CampaignSummary(
        campaign_id=campaign_id,
        detected=int(data.get("detected", 0) or 0),
        denominator=int(data.get("denominator", 0) or 0),
        undetected=int(data.get("undetected", 0) or 0),
        fault_coverage_percent=float(data.get("fault_coverage_percent", 0.0) or 0.0),
        test_coverage_percent=float(data.get("test_coverage_percent", 0.0) or 0.0),
        vector_count=int(run.get("vector_count", 0) or 0),
        atpg_seconds=float(run.get("atpg_generation_seconds", 0.0) or 0.0),
        sim_seconds=float(run.get("fault_simulation_seconds", 0.0) or 0.0),
        total_seconds=float(run.get("total_sim_seconds", 0.0) or 0.0),
        atpg_rounds=int(run.get("atpg_rounds", 0) or 0),
    )


def compute_rescued(conn: sqlite3.Connection, baseline_id: int, tp_id: int) -> int:
    """Faults undetected in baseline but detected in TP, excluding inserted TP nets."""
    row = conn.execute(
        """
        SELECT COUNT(*) FROM faults b
        JOIN faults t
          ON b.net_name = t.net_name AND b.fault_type = t.fault_type
        WHERE b.campaign_id = ? AND t.campaign_id = ?
          AND b.status = 'undetected'
          AND b.collapsed_into IS NULL
          AND b.exclusion = 'none'
          AND t.status = 'detected'
          AND b.net_name NOT LIKE 'tp_obs_%'
          AND b.net_name NOT LIKE 'tp_ctrl_%'
        """,
        (baseline_id, tp_id),
    ).fetchone()
    return int(row[0]) if row else 0


def build_comparison(
    baseline: CampaignSummary,
    tp: CampaignSummary,
    rescued: int,
    *,
    tp_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a serializable comparison dict (Baseline | +TP | Δ)."""

    def _d(a: float, b: float) -> float:
        return round(b - a, 3)

    info = tp_report or {}
    return {
        "fault_coverage_pct": {
            "baseline": round(baseline.fault_coverage_percent, 3),
            "tp": round(tp.fault_coverage_percent, 3),
            "delta": _d(baseline.fault_coverage_percent, tp.fault_coverage_percent),
        },
        "test_coverage_pct": {
            "baseline": round(baseline.test_coverage_percent, 3),
            "tp": round(tp.test_coverage_percent, 3),
            "delta": _d(baseline.test_coverage_percent, tp.test_coverage_percent),
        },
        "detected": {
            "baseline": baseline.detected,
            "tp": tp.detected,
            "delta": tp.detected - baseline.detected,
        },
        "denominator": {
            "baseline": baseline.denominator,
            "tp": tp.denominator,
            "delta": tp.denominator - baseline.denominator,
        },
        "undetected": {
            "baseline": baseline.undetected,
            "tp": tp.undetected,
            "delta": tp.undetected - baseline.undetected,
        },
        "original_rescued": rescued,
        "vector_count": {
            "baseline": baseline.vector_count,
            "tp": tp.vector_count,
            "delta": tp.vector_count - baseline.vector_count,
        },
        "atpg_seconds": {
            "baseline": round(baseline.atpg_seconds, 2),
            "tp": round(tp.atpg_seconds, 2),
            "delta": _d(baseline.atpg_seconds, tp.atpg_seconds),
        },
        "sim_seconds": {
            "baseline": round(baseline.sim_seconds, 2),
            "tp": round(tp.sim_seconds, 2),
            "delta": _d(baseline.sim_seconds, tp.sim_seconds),
        },
        "total_seconds": {
            "baseline": round(baseline.total_seconds, 2),
            "tp": round(tp.total_seconds, 2),
            "delta": _d(baseline.total_seconds, tp.total_seconds),
        },
        "atpg_rounds": {
            "baseline": baseline.atpg_rounds,
            "tp": tp.atpg_rounds,
            "delta": tp.atpg_rounds - baseline.atpg_rounds,
        },
        "tp_inserted": {
            "total": int(info.get("total", 0)),
            "obs": int(info.get("obs", 0)),
            "ctrl": int(info.get("ctrl", 0)),
        },
    }
