"""Chip-level (SoC) coverage report for a hierarchical project.

Unlike `reporter/unified.py` (which shows scan + comb side-by-side and explicitly
does NOT add their counts because the fault universes overlap), this report
**adds** scope counts: per the ownership model, block INTEST and assembly EXTEST
own disjoint, complete fault sets, so a real additive chip number is valid.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from faultflow.project.aggregate import ChipCoverage

SOC_COVERAGE_SCHEMA = "faultflow_soc_coverage_v1"


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
        "scopes": [asdict(s) for s in chip.scopes],
        "guards": chip.guards,
    }


def _validate_report(report: dict[str, Any]) -> None:
    schema_path = Path("schemas/soc_coverage.schema.json")
    if not schema_path.exists():
        raise SocReportError(f"missing SoC coverage schema: {schema_path}")
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - local fallback for minimal envs
        _validate_report_shape(report)
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema).validate(report)
    except Exception as exc:  # jsonschema.ValidationError
        raise SocReportError(f"SoC coverage report failed schema validation: {exc}")


def _validate_report_shape(report: dict[str, Any]) -> None:
    required = {"schema", "project", "chip", "scopes", "guards"}
    missing = required - set(report)
    if missing:
        raise SocReportError(f"SoC coverage report missing keys: {sorted(missing)}")
    chip_required = {"denominator", "detected", "coverage_percent"}
    chip = report["chip"]
    if not isinstance(chip, dict) or chip_required - set(chip):
        raise SocReportError("SoC coverage report 'chip' section is malformed")
    if not isinstance(chip["denominator"], int) or isinstance(
        chip["denominator"], bool
    ):
        raise SocReportError("SoC coverage report chip.denominator must be an int")
    if not isinstance(chip["detected"], int) or isinstance(chip["detected"], bool):
        raise SocReportError("SoC coverage report chip.detected must be an int")
    if not isinstance(report["scopes"], list):
        raise SocReportError("SoC coverage report 'scopes' must be an array")
    if not isinstance(report["guards"], dict):
        raise SocReportError("SoC coverage report 'guards' must be an object")


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.3f}%"


def write_soc_report(chip: ChipCoverage, out_dir: Path) -> tuple[Path, Path]:
    """Write the SoC coverage JSON + text report; return (json_path, txt_path)."""
    report = soc_report_dict(chip)
    _validate_report(report)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "soc_coverage.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"faultflow SoC coverage report for {chip.project}",
        "",
        "chip (additive — scopes own disjoint, complete fault sets):",
        f"  denominator:      {chip.chip_denominator}",
        f"  detected:         {chip.chip_detected}",
        f"  coverage_percent: {_fmt_pct(chip.chip_coverage_percent)}",
        "",
        "scopes:",
    ]
    for s in chip.scopes:
        lines.extend(
            [
                f"  [{s.kind}] {s.name}  (top={s.top}, campaign={s.campaign_id})",
                f"      own_coverage={_fmt_pct(s.coverage_percent)}  "
                f"(scope denom={s.denominator} detected={s.detected})",
                f"      chip_owned={s.owned}  owned_detected={s.owned_detected}  "
                f"foreign={s.foreign}  handoff={s.handoff}  "
                f"excluded={s.excluded_by_design}  total={s.total_sites}",
            ]
        )
    lines.extend(
        [
            "",
            "guards:",
            f"  tops_disjoint:    {chip.guards.get('tops_disjoint')}",
            f"  no_double_count:  {chip.guards.get('no_double_count')}",
            f"  partition_total:  {chip.guards.get('partition_total')}",
            f"  handoff_complete: {chip.guards.get('handoff_complete')}",
            f"  owned_sites:      {chip.guards.get('owned_sites', 0)}",
            f"  handoff_sites:    {chip.guards.get('handoff_sites', 0)}",
            "",
        ]
    )
    txt_path = out_dir / "soc_coverage.rpt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, txt_path
