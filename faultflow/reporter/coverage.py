from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from faultflow.db import latest_campaign_id, summary


class CoverageError(RuntimeError):
    pass


def _fingerprint(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _per_node(conn: sqlite3.Connection, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
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
        WHERE campaign_id = ?
        GROUP BY net_id
        ORDER BY net_id
        """,
        (campaign_id,),
    ).fetchall()
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


def _policy(fp: dict[str, Any]) -> dict[str, Any]:
    return {
        "unsupported_cells": fp.get("unsupported_cells", "fail"),
        "include_clock_faults": bool(fp.get("include_clock_faults", 0)),
        "include_reset_faults": bool(fp.get("include_reset_faults", 0)),
        "collapsing": bool(fp.get("collapsing", 0)),
    }


def _undetected_faults(
    conn: sqlite3.Connection, campaign_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, net_id, net_name, fault_type, fault_site_key, protocol_unresolved
        FROM faults
        WHERE campaign_id = ?
          AND status = 'undetected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
        ORDER BY net_id, fault_type
        """,
        (campaign_id,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "net_id": int(row["net_id"]),
            "net_name": row["net_name"],
            "fault_type": row["fault_type"],
            "fault_site_key": row["fault_site_key"],
            "protocol_unresolved": bool(row["protocol_unresolved"]),
        }
        for row in rows
    ]


def _latest_run(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, vector_source, vector_count, atpg_generation_seconds,
               fault_simulation_seconds, total_sim_seconds, coverage,
               atpg_terminal_reason, atpg_rounds, atpg_sat, atpg_unsat,
               atpg_timeout, atpg_unknown, atpg_rejected_candidates,
               atpg_generated_vectors, atpg_accepted_vectors,
               protocol_no_progress_rounds, candidates_aborted
        FROM runs
        WHERE campaign_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
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
        "structural_eligible",
        "denominator",
        "detected",
        "undetected",
        "redundant",
        "collapsed",
        "excluded_blackbox",
        "excluded_clock",
        "excluded_reset",
        "protocol_unresolved",
        "fault_coverage_percent",
        "test_coverage_percent",
        "coverage_percent",
    }
    summary_missing = summary_required - set(report["summary"])
    if summary_missing:
        raise CoverageError(f"coverage summary missing keys: {sorted(summary_missing)}")
    if not isinstance(report["per_node"], list) or not isinstance(
        report["undetected_faults"], list
    ):
        raise CoverageError("coverage report node/fault sections must be arrays")


def _translate_scan_net_name(
    net_name: str, pseudo_port_map: dict[str, dict[str, Any]]
) -> str:
    if net_name.startswith("__ppi_"):
        instance = net_name.removeprefix("__ppi_")
        return f"{instance}.Q"
    if net_name.startswith("__ppo_"):
        instance = net_name.removeprefix("__ppo_")
        return f"{instance}.D"
    return net_name


def write_reports(
    conn: sqlite3.Connection,
    output_dir: Path,
    top: str,
    scan_context: dict[str, Any] | None = None,
    campaign_id: int | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if campaign_id is None:
        campaign_type = "scan" if scan_context is not None else "comb"
        resolved = latest_campaign_id(conn, campaign_type)
        if resolved is None:
            raise CoverageError(f"no {campaign_type} campaign in database")
        campaign_id = resolved
    data = summary(conn, campaign_id=campaign_id)
    if data["denominator"] == 0:
        raise CoverageError("denominator is zero")
    invariant = (
        data["denominator"]
        + data.get("redundant", 0)
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

    fp = _fingerprint(conn, campaign_id)
    per_node = _per_node(conn, campaign_id)
    undetected = _undetected_faults(conn, campaign_id)
    if scan_context is not None:
        pseudo_port_map = scan_context.get("pseudo_port_map", {})
        if isinstance(pseudo_port_map, dict):
            for node in per_node:
                node["net_name"] = _translate_scan_net_name(
                    str(node["net_name"]), pseudo_port_map
                )
            for fault in undetected:
                fault["net_name"] = _translate_scan_net_name(
                    str(fault["net_name"]), pseudo_port_map
                )

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
        "run": _latest_run(conn, campaign_id),
        "per_node": per_node,
        "undetected_faults": undetected,
    }
    if scan_context is not None:
        run = cast(dict[str, Any], report["run"])
        run["scan_mode"] = True
        run["scan_manifest_hash"] = scan_context.get("manifest_hash", "")
    _validate_report(report)
    run = cast(dict[str, Any], report["run"])
    json_path = output_dir / "coverage_report.json"
    txt_path = output_dir / "fault_report.txt"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    txt = [
        f"faultflow coverage report for {top}",
    ]
    if scan_context is not None:
        txt.extend(
            [
                "scan_mode:           true",
                "excluded_faults:     scan-cell-internal, scan-path-only (Q->SDI)",
                "",
            ]
        )
    txt.extend(
        [
            "",
            f"total_raw_faults:    {data['total_raw_faults']}",
            f"structural_eligible: {data['structural_eligible']}",
            f"denominator:         {data['denominator']}",
            f"detected:            {data['detected']}",
            f"undetected:          {data['undetected']}",
            f"redundant:           {data.get('redundant', 0)}",
            f"collapsed:           {data['collapsed']}",
            f"excluded_blackbox:   {data['excluded_blackbox']}",
            f"excluded_clock:      {data['excluded_clock']}",
            f"excluded_reset:      {data['excluded_reset']}",
            f"protocol_unresolved: {data['protocol_unresolved']}",
            f"fault_coverage_%:    {data['fault_coverage_percent']:.3f}",
            f"test_coverage_%:     {data['test_coverage_percent']:.3f}",
            f"coverage_percent:    {data['coverage_percent']:.3f}",
            "",
            "run:",
            f"vector_source:       {run.get('vector_source', '')}",
            f"vector_count:        {run.get('vector_count', 0)}",
            "atpg_seconds:        "
            f"{float(run.get('atpg_generation_seconds', 0.0)):.3f}",
            "fault_sim_seconds:   "
            f"{float(run.get('fault_simulation_seconds', 0.0)):.3f}",
            "total_sim_seconds:   " f"{float(run.get('total_sim_seconds', 0.0)):.3f}",
            f"atpg_terminal:       {run.get('atpg_terminal_reason', '') or 'n/a'}",
            f"atpg_rounds:         {run.get('atpg_rounds', 0)}",
            f"atpg_sat:            {run.get('atpg_sat', 0)}",
            f"atpg_unsat:          {run.get('atpg_unsat', 0)}",
            f"atpg_timeout:        {run.get('atpg_timeout', 0)}",
            f"atpg_unknown:        {run.get('atpg_unknown', 0)}",
            f"atpg_rejected:       {run.get('atpg_rejected_candidates', 0)}",
            "",
            "policy:",
            f"unsupported_cells:   {_policy_text(report, 'unsupported_cells')}",
            f"include_clock_faults:{_policy_text(report, 'include_clock_faults')}",
            f"include_reset_faults:{_policy_text(report, 'include_reset_faults')}",
            f"collapsing:          {_policy_text(report, 'collapsing')}",
            "",
            "undetected faults:",
        ]
    )
    for fault in cast(list[dict[str, Any]], report["undetected_faults"]):
        proto = " protocol_unresolved" if fault.get("protocol_unresolved") else ""
        txt.append(
            f"- id={fault['id']} net={fault['net_id']} "
            f"name={fault['net_name']} type={fault['fault_type']}"
            f" site={fault.get('fault_site_key', '')}{proto}"
        )
    txt.append("")
    txt_path.write_text("\n".join(txt), encoding="utf-8")
    return json_path, txt_path, report


def _policy_text(report: dict[str, Any], key: str) -> Any:
    return cast(dict[str, Any], report["policy"])[key]
