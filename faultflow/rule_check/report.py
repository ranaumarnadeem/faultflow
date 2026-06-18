"""Render a RuleCheckReport to text and JSON."""

from __future__ import annotations

import json
from pathlib import Path

from faultflow.rule_check.model import RuleCheckReport, Severity


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


def write_reports(
    report: RuleCheckReport,
    txt_path: Path,
    json_path: Path,
    strict: bool = False,
) -> None:
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(format_text(report, strict=strict), encoding="utf-8")
    json_path.write_text(
        json.dumps(to_json(report, strict=strict), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
