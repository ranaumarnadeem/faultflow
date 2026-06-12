from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from faultflow.db import summary


class CoverageError(RuntimeError):
    pass


def _fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM design_fingerprint WHERE id = 1").fetchone()
    return dict(row) if row is not None else {}


def _policy(fp: dict[str, Any]) -> dict[str, Any]:
    return {
        "unsupported_cells": fp.get("unsupported_cells", "fail"),
        "include_clock_faults": bool(fp.get("include_clock_faults", 0)),
        "include_reset_faults": bool(fp.get("include_reset_faults", 0)),
        "collapsing": bool(fp.get("collapsing", 0)),
    }


def _per_node(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT
          net_id,
          MIN(net_name) AS net_name,
          COUNT(*) AS total_faults,
          SUM(CASE WHEN status = 'detected' THEN 1 ELSE 0 END) AS detected,
          SUM(CASE WHEN status = 'undetected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS undetected,
          SUM(CASE WHEN collapsed_into IS NOT NULL THEN 1 ELSE 0 END) AS collapsed,
          SUM(CASE WHEN exclusion != 'none' THEN 1 ELSE 0 END) AS excluded
        FROM faults
        GROUP BY net_id
        ORDER BY net_id
        """).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        total = int(row["total_faults"] or 0)
        detected = int(row["detected"] or 0)
        eligible = total - int(row["collapsed"] or 0) - int(row["excluded"] or 0)
        coverage = None if eligible == 0 else 100.0 * detected / eligible
        nodes.append(
            {
                "net_id": int(row["net_id"]),
                "net_name": row["net_name"],
                "total_faults": total,
                "detected": detected,
                "undetected": int(row["undetected"] or 0),
                "collapsed": int(row["collapsed"] or 0),
                "excluded": int(row["excluded"] or 0),
                "coverage_percent": coverage,
            }
        )
    return nodes


def _undetected_faults(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT id, net_id, net_name, fault_type
        FROM faults
        WHERE status = 'undetected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
        ORDER BY net_id, fault_type
        """).fetchall()
    return [
        {
            "id": int(row["id"]),
            "net_id": int(row["net_id"]),
            "net_name": row["net_name"],
            "fault_type": row["fault_type"],
        }
        for row in rows
    ]


def _latest_run(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("""
        SELECT id, vector_source, vector_count, atpg_generation_seconds,
               fault_simulation_seconds, total_sim_seconds
        FROM runs
        ORDER BY id DESC
        LIMIT 1
        """).fetchone()
    return dict(row) if row is not None else {}


def _validate_report(report: dict[str, Any]) -> None:
    schema_path = Path("schemas/coverage.schema.json")
    if not schema_path.exists():
        raise CoverageError(f"missing coverage schema: {schema_path}")
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - local fallback for minimal envs
        _validate_report_shape(report)
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)


def _validate_report_shape(report: dict[str, Any]) -> None:
    required = {
        "metadata",
        "policy",
        "summary",
        "run",
        "per_node",
        "undetected_faults",
    }
    missing = required - set(report)
    if missing:
        raise CoverageError(f"coverage report missing keys: {sorted(missing)}")
    summary_required = {
        "total_raw_faults",
        "denominator",
        "detected",
        "undetected",
        "collapsed",
        "excluded_blackbox",
        "excluded_clock",
        "excluded_reset",
        "coverage_percent",
    }
    summary_missing = summary_required - set(report["summary"])
    if summary_missing:
        raise CoverageError(f"coverage summary missing keys: {sorted(summary_missing)}")
    if not isinstance(report["per_node"], list) or not isinstance(
        report["undetected_faults"], list
    ):
        raise CoverageError("coverage report node/fault sections must be arrays")


def write_reports(
    conn: sqlite3.Connection, output_dir: Path, top: str
) -> tuple[Path, Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = summary(conn)
    if data["denominator"] == 0:
        raise CoverageError("denominator is zero")
    invariant = (
        data["denominator"]
        + data["collapsed"]
        + data["excluded_blackbox"]
        + data["excluded_clock"]
        + data["excluded_reset"]
    )
    if data["total_raw_faults"] != invariant:
        raise CoverageError(
            "coverage denominator invariant failed: "
            f"total_raw_faults={data['total_raw_faults']} invariant={invariant}"
        )

    fp = _fingerprint(conn)
    report = {
        "metadata": {
            "top": top,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "faultflow_version": fp.get("faultflow_version", "pipeline-v1"),
            "yosys_version": fp.get("yosys_version", ""),
            "netlist_hash": fp.get("netlist_hash", ""),
            "cell_lib_hash": fp.get("cell_lib_hash", ""),
            "config_hash": fp.get("config_hash", ""),
            "template_hash": fp.get("template_hash", ""),
        },
        "policy": _policy(fp),
        "summary": data,
        "run": _latest_run(conn),
        "per_node": _per_node(conn),
        "undetected_faults": _undetected_faults(conn),
    }
    _validate_report(report)
    run = cast(dict[str, Any], report["run"])
    json_path = output_dir / "coverage_report.json"
    txt_path = output_dir / "fault_report.txt"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    txt = [
        f"faultflow coverage report for {top}",
        "",
        f"total_raw_faults:    {data['total_raw_faults']}",
        f"denominator:         {data['denominator']}",
        f"detected:            {data['detected']}",
        f"undetected:          {data['undetected']}",
        f"collapsed:           {data['collapsed']}",
        f"excluded_blackbox:   {data['excluded_blackbox']}",
        f"excluded_clock:      {data['excluded_clock']}",
        f"excluded_reset:      {data['excluded_reset']}",
        f"coverage_percent:    {data['coverage_percent']:.3f}",
        "",
        "run:",
        f"vector_source:       {run.get('vector_source', '')}",
        f"vector_count:        {run.get('vector_count', 0)}",
        "atpg_seconds:        " f"{float(run.get('atpg_generation_seconds', 0.0)):.3f}",
        "fault_sim_seconds:   "
        f"{float(run.get('fault_simulation_seconds', 0.0)):.3f}",
        "total_sim_seconds:   " f"{float(run.get('total_sim_seconds', 0.0)):.3f}",
        "",
        "policy:",
        f"unsupported_cells:   {_policy_text(report, 'unsupported_cells')}",
        f"include_clock_faults:{_policy_text(report, 'include_clock_faults')}",
        f"include_reset_faults:{_policy_text(report, 'include_reset_faults')}",
        f"collapsing:          {_policy_text(report, 'collapsing')}",
        "",
        "undetected faults:",
    ]
    for fault in cast(list[dict[str, Any]], report["undetected_faults"]):
        txt.append(
            f"- id={fault['id']} net={fault['net_id']} "
            f"name={fault['net_name']} type={fault['fault_type']}"
        )
    txt.append("")
    txt_path.write_text("\n".join(txt), encoding="utf-8")
    return json_path, txt_path, report


def _policy_text(report: dict[str, Any], key: str) -> Any:
    return cast(dict[str, Any], report["policy"])[key]
