"""The chip coverage report of a faultflow_project_v2 SoC: one number over every
block's INTEST and the SoC's EXTEST, each fault counted once by its identities
(faultflow.project.soc_aggregate), and the guards that make it right.

Unlike `reporter/unified.py`, which shows scan and combinational campaigns side by
side without adding them, this report adds its scopes: they own disjoint fault
sets, and every fault one leaves to another is graded there or accounted for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.project.soc_aggregate import ChipCoverage

SOC_COVERAGE_SCHEMA = "faultflow_soc_coverage_v2"

# Ships with the package: found relative to this file, from any working directory.
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/soc_coverage.schema.json"


class SocReportError(RuntimeError):
    pass


def soc_report_dict(chip: ChipCoverage) -> dict[str, object]:
    return {
        "schema": SOC_COVERAGE_SCHEMA,
        "project": chip.project,
        "chip": {
            "denominator": chip.chip_denominator,
            "detected": chip.chip_detected,
            "coverage_percent": chip.chip_coverage_percent,
        },
        "scopes": [
            {
                "name": scope.name,
                "campaign_id": scope.campaign_id,
                "owned": scope.owned,
                "owned_detected": scope.owned_detected,
                "handoff": scope.handoff,
                "excluded": scope.excluded,
                "total": scope.total,
                "coverage_percent": scope.coverage_percent,
            }
            for scope in chip.scopes
        ],
        "guards": chip.guards,
    }


def _validate_report(report: dict[str, Any]) -> None:
    if not _SCHEMA_PATH.exists():
        raise SocReportError(f"missing SoC coverage schema: {_SCHEMA_PATH}")
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - local fallback for minimal envs
        _validate_report_shape(report)
        return
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema).validate(report)
    except Exception as exc:  # jsonschema.ValidationError
        raise SocReportError(f"SoC coverage report failed schema validation: {exc}")


def _validate_report_shape(report: dict[str, Any]) -> None:
    required = {"schema", "project", "chip", "scopes", "guards"}
    missing = required - set(report)
    if missing:
        raise SocReportError(f"SoC coverage report missing keys: {sorted(missing)}")
    chip = report["chip"]
    if not isinstance(chip, dict) or {
        "denominator",
        "detected",
        "coverage_percent",
    } - set(chip):
        raise SocReportError("SoC coverage report 'chip' section is malformed")
    for key in ("denominator", "detected"):
        if not isinstance(chip[key], int) or isinstance(chip[key], bool):
            raise SocReportError(f"SoC coverage report chip.{key} must be an int")
    if not isinstance(report["scopes"], list):
        raise SocReportError("SoC coverage report 'scopes' must be an array")
    if not isinstance(report["guards"], dict):
        raise SocReportError("SoC coverage report 'guards' must be an object")


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.3f}%"


def write_soc_report(chip: ChipCoverage, out_dir: Path) -> tuple[Path, Path]:
    """Write the SoC coverage JSON and text report; (json_path, txt_path)."""
    report = soc_report_dict(chip)
    _validate_report(report)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "soc_coverage.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"faultflow SoC coverage report for {chip.project}",
        "",
        "chip (each fault once, in the scope whose test grades it):",
        f"  denominator:      {chip.chip_denominator}",
        f"  detected:         {chip.chip_detected}",
        f"  coverage_percent: {_percent(chip.chip_coverage_percent)}",
        "",
        "scopes: owned (detected), left to the other mode, excluded, total",
    ]
    for scope in chip.scopes:
        lines.append(
            f"  {scope.name:12s} {scope.owned:6d} ({scope.owned_detected:6d}) "
            f"{_percent(scope.coverage_percent):>9s}  {scope.handoff:6d}  "
            f"{scope.excluded:6d}  {scope.total:6d}"
        )
    lines += ["", "guards:"]
    lines += [f"  {key}: {value}" for key, value in chip.guards.items()]
    txt_path = out_dir / "soc_coverage.rpt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, txt_path
