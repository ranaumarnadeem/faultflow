"""Schema enforcement for the SoC (hierarchical project) chip coverage report.

`write_soc_report` (faultflow/reporter/soc.py) was, prior to this test, the only
report writer with zero schema validation -- compare `coverage.py`'s
`_validate_report` gate on `schemas/coverage.schema.json`. These tests pin the
same behavior for `schemas/soc_coverage.schema.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.project.aggregate import ChipCoverage, ScopeCoverage
from faultflow.reporter.soc import SocReportError, soc_report_dict, write_soc_report


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


def test_write_soc_report_validates_from_any_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs from an arbitrary cwd with NO schema copied alongside -- the schema is
    resolved relative to the package, not cwd. A cwd-relative Path("schemas/...")
    crashed `project` from outside the repo root (found in the e2e bug hunt)."""
    monkeypatch.chdir(tmp_path)
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
    chip = _sample_chip()
    chip.chip_denominator = "not-an-int"  # type: ignore[assignment]

    out_dir = tmp_path / "output" / "soc2"
    with pytest.raises(Exception):
        write_soc_report(chip, out_dir)

    assert not (out_dir / "soc_coverage.json").exists()


def test_write_soc_report_missing_schema_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the shipped schema is somehow absent (broken packaging), fail loudly.
    The schema now resolves relative to the package, so force the branch by
    pointing _SCHEMA_PATH at a nonexistent file rather than relying on cwd."""
    monkeypatch.setattr(
        "faultflow.reporter.soc._SCHEMA_PATH", tmp_path / "nonexistent.json"
    )
    chip = _sample_chip()

    out_dir = tmp_path / "output" / "soc2"
    with pytest.raises(SocReportError, match="missing SoC coverage schema"):
        write_soc_report(chip, out_dir)

    assert not (out_dir / "soc_coverage.json").exists()


# --------------------------------------------------------------------------- #
# _validate_report_shape: the manual fallback used when jsonschema is not     #
# installed (a genuinely optional dependency). Exercised directly rather than #
# by uninstalling jsonschema, since it's a plain, independently testable      #
# function -- see faultflow/reporter/coverage.py for the identical pattern    #
# this mirrors.                                                               #
# --------------------------------------------------------------------------- #


def test_validate_report_shape_rejects_missing_top_level_key() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    del report["guards"]

    with pytest.raises(SocReportError, match="missing keys"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_malformed_chip_section() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    del report["chip"]["coverage_percent"]

    with pytest.raises(SocReportError, match="'chip' section is malformed"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_non_int_denominator() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    report["chip"]["denominator"] = "120"

    with pytest.raises(SocReportError, match="denominator must be an int"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_non_int_detected() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    report["chip"]["detected"] = "110"

    with pytest.raises(SocReportError, match="detected must be an int"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_non_list_scopes() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    report["scopes"] = {}

    with pytest.raises(SocReportError, match="'scopes' must be an array"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_non_dict_guards() -> None:
    from faultflow.reporter.soc import SocReportError, _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))
    report["guards"] = []

    with pytest.raises(SocReportError, match="'guards' must be an object"):
        _validate_report_shape(report)


def test_validate_report_shape_accepts_well_formed_report() -> None:
    from faultflow.reporter.soc import _validate_report_shape

    report = json.loads(json.dumps(soc_report_dict(_sample_chip())))

    _validate_report_shape(report)  # must not raise
