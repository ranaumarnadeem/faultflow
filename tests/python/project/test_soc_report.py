"""Schema enforcement for the SoC (hierarchical project) chip coverage report.

`write_soc_report` (faultflow/reporter/soc.py) was, prior to this test, the only
report writer with zero schema validation -- compare `coverage.py`'s
`_validate_report` gate on `schemas/coverage.schema.json`. These tests pin the
same behavior for `schemas/soc_coverage.schema.json`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.project.aggregate import ChipCoverage, ScopeCoverage
from faultflow.reporter.soc import write_soc_report

_SCHEMA_SRC = Path(__file__).resolve().parents[3] / "schemas/soc_coverage.schema.json"


def _copy_schema(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_SCHEMA_SRC, schema_dir / "soc_coverage.schema.json")


def _sample_chip() -> ChipCoverage:
    scope_a = ScopeCoverage(
        kind="block",
        name="blkA",
        top="blkA",
        campaign_id=1,
        denominator=100,
        detected=90,
        owned=100,
        owned_detected=90,
        foreign=0,
        handoff=5,
        excluded_by_design=3,
        total_sites=108,
        coverage_percent=90.0,
    )
    scope_b = ScopeCoverage(
        kind="interconnect",
        name="assembly",
        top="soc_top",
        campaign_id=2,
        denominator=20,
        detected=20,
        owned=20,
        owned_detected=20,
        foreign=0,
        handoff=0,
        excluded_by_design=0,
        total_sites=20,
        coverage_percent=100.0,
    )
    chip = ChipCoverage(project="soc2")
    chip.scopes = [scope_a, scope_b]
    chip.chip_denominator = scope_a.owned + scope_b.owned
    chip.chip_detected = scope_a.owned_detected + scope_b.owned_detected
    chip.chip_coverage_percent = 100.0 * chip.chip_detected / chip.chip_denominator
    chip.guards = {
        "tops_disjoint": True,
        "no_double_count": True,
        "partition_total": True,
        "handoff_complete": True,
        "owned_sites": 120,
        "handoff_sites": 5,
        "accounted_sites": 3,
    }
    return chip


def test_write_soc_report_validates_against_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _copy_schema(tmp_path)
    chip = _sample_chip()

    json_path, txt_path = write_soc_report(chip, tmp_path / "output" / "soc2")

    assert json_path.exists()
    assert txt_path.exists()
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["schema"] == "faultflow_soc_coverage_v1"
    assert report["chip"]["denominator"] == chip.chip_denominator


def test_write_soc_report_rejects_malformed_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ChipCoverage whose chip_denominator is a str (schema violation) must be
    rejected before anything is written -- never silently emit a bad report."""
    monkeypatch.chdir(tmp_path)
    _copy_schema(tmp_path)
    chip = _sample_chip()
    chip.chip_denominator = "not-an-int"  # type: ignore[assignment]

    out_dir = tmp_path / "output" / "soc2"
    with pytest.raises(Exception):
        write_soc_report(chip, out_dir)

    assert not (out_dir / "soc_coverage.json").exists()


def test_write_soc_report_missing_schema_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No schemas/soc_coverage.schema.json on disk -- fail loudly, not silently."""
    monkeypatch.chdir(tmp_path)
    chip = _sample_chip()

    out_dir = tmp_path / "output" / "soc2"
    with pytest.raises(Exception):
        write_soc_report(chip, out_dir)

    assert not (out_dir / "soc_coverage.json").exists()
