"""Render a RuleCheckReport to text and JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.rule_check.model import RuleCheckReport, Severity

_REPO_ROOT = Path(__file__).resolve().parents[2]


class RuleCheckReportError(RuntimeError):
    pass


def format_text(report: RuleCheckReport, strict: bool = False) -> str:
    status = "PASS" if report.passed(strict=strict) else "FAIL"
    lines = [
        f"faultflow rule_check (DFT DRC) for {report.top}",
        f"status: {status}   "
        f"errors={len(report.errors)} warnings={len(report.warnings)} "
        f"info={len(report.infos)}",
    ]
    if not report.violations:
        lines.append("no rule violations")
        return "\n".join(lines) + "\n"
    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    for v in sorted(report.violations, key=lambda v: (order[v.severity], v.rule_id)):
        lines.append(v.format_line())
    return "\n".join(lines) + "\n"


def to_json(report: RuleCheckReport, strict: bool = False) -> dict[str, object]:
    return {
        "version": 1,
        "top": report.top,
        "status": "PASS" if report.passed(strict=strict) else "FAIL",
        "summary": {
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "info": len(report.infos),
        },
        "violations": [
            {
                "rule_id": v.rule_id,
                "severity": v.severity.value,
                "title": v.title,
                "message": v.message,
            }
            for v in report.violations
        ],
    }


def _validate_report(report: dict[str, Any]) -> None:
    schema_path = _REPO_ROOT / "schemas" / "rule_check.schema.json"
    if not schema_path.exists():
        raise RuleCheckReportError(f"missing rule_check schema: {schema_path}")
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - local fallback for minimal envs
        _validate_report_shape(report)
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)


def _validate_report_shape(report: dict[str, Any]) -> None:
    required = {"version", "top", "status", "summary", "violations"}
    missing = required - set(report)
    if missing:
        raise RuleCheckReportError(f"rule_check report missing keys: {sorted(missing)}")
    summary_required = {"errors", "warnings", "info"}
    summary_missing = summary_required - set(report["summary"])
    if summary_missing:
        raise RuleCheckReportError(
            f"rule_check summary missing keys: {sorted(summary_missing)}"
        )
    if not isinstance(report["violations"], list):
        raise RuleCheckReportError("rule_check report violations must be an array")


def write_reports(
    report: RuleCheckReport,
    txt_path: Path,
    json_path: Path,
    strict: bool = False,
) -> None:
    payload = to_json(report, strict=strict)
    _validate_report(payload)
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(format_text(report, strict=strict), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
