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

from faultflow.project.aggregate import ChipCoverage

SOC_COVERAGE_SCHEMA = "faultflow_soc_coverage_v1"


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


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.3f}%"


def write_soc_report(chip: ChipCoverage, out_dir: Path) -> tuple[Path, Path]:
    """Write the SoC coverage JSON + text report; return (json_path, txt_path)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = soc_report_dict(chip)

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
