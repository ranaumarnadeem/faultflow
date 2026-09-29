"""`schemas/coverage.schema.json`'s `policy` object must accept the
`blackbox_instances`/`blackbox_boundary` fields `faultflow/reporter/coverage.py`'s
`_policy()` always emits.

Found via a real bug: the schema had `additionalProperties: false` on `policy` but
never declared those two keys, so EVERY `write_reports()` call failed schema
validation the moment `jsonschema` was actually installed (it had been silently
skipped as an optional dependency up to that point -- see `_validate_report`'s
ImportError soft-skip path). This validates the `policy` sub-schema directly and
in isolation (not the whole top-level report, which has many unrelated required
fields already covered end-to-end by tests/python/cli/test_cli_flow.py's schema
tests and the `unsupported_cells = blackbox` project fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/coverage.schema.json"


def _policy_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["properties"]["policy"]  # type: ignore[no-any-return]


def _policy(*, blackbox_instances: list[str], blackbox_boundary: str) -> dict:
    return {
        "unsupported_cells": "blackbox" if blackbox_instances else "fail",
        "include_clock_faults": False,
        "include_reset_faults": False,
        "collapsing": False,
        "blackbox_instances": blackbox_instances,
        "blackbox_boundary": blackbox_boundary,
    }


def test_schema_accepts_blackbox_policy_fields() -> None:
    policy = _policy(blackbox_instances=["u_core"], blackbox_boundary="pseudo_port")

    jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_accepts_no_blackbox_policy_fields() -> None:
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")

    jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_accepts_opaque_blackbox_boundary() -> None:
    policy = _policy(blackbox_instances=["u_sram"], blackbox_boundary="opaque")

    jsonschema.validate(instance=policy, schema=_policy_schema())


@pytest.mark.parametrize(
    ("campaign_type", "instances", "expected"),
    [
        ("scan", ("u_sram",), "opaque"),
        ("comb", ("u_sram",), "pseudo_port"),
        ("scan_extest", ("u_sram",), "pseudo_port"),
        ("scan", (), "none"),
        ("comb", (), "none"),
    ],
)
def test_policy_reports_the_blackbox_model_the_campaign_used(
    campaign_type: str, instances: tuple[str, ...], expected: str
) -> None:
    """A scan campaign's reduced view models a blackbox opaque (outputs tied to
    0, inputs unobserved); combinational and EXTEST ATPG model it as a pseudo
    port. The report must say which one the numbers came from."""
    from faultflow.reporter.coverage import _policy as report_policy

    policy = report_policy({"campaign_type": campaign_type}, instances)

    assert policy["blackbox_boundary"] == expected
    jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_rejects_invalid_blackbox_boundary_value() -> None:
    policy = _policy(blackbox_instances=["u_core"], blackbox_boundary="bogus")

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_requires_blackbox_boundary_field() -> None:
    """`_policy()` always emits both keys -- the schema should require them, not
    just tolerate them, so a future regression that drops one is caught."""
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")
    del policy["blackbox_boundary"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_requires_blackbox_instances_field() -> None:
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")
    del policy["blackbox_instances"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())


def _minimal_valid_report() -> dict[str, Any]:
    return {
        "metadata": {
            "top": "demo",
            "generated_at": "2026-01-01T00:00:00Z",
            "faultflow_version": "0",
            "yosys_version": "0",
            "netlist_hash": "h",
            "cell_lib_hash": "h",
            "config_hash": "h",
            "template_hash": "h",
        },
        "policy": _policy(blackbox_instances=[], blackbox_boundary="none"),
        "summary": {
            "total_raw_faults": 1,
            "structural_eligible": 1,
            "denominator": 1,
            "detected": 1,
            "undetected": 0,
            "redundant": 0,
            "collapsed": 0,
            "excluded_blackbox": 0,
            "excluded_clock": 0,
            "excluded_reset": 0,
            "excluded_scan": 0,
            "excluded_scan_internal": 0,
            "excluded_scan_chain": 0,
            "excluded_cross_domain": 0,
            "excluded_wbr_decoupled": 0,
            "excluded_jtag": 0,
            "protocol_unresolved": 0,
            "compression_unresolved": 0,
            "compaction_unresolved": 0,
            "blackbox_unresolved": 0,
            "fault_coverage_percent": 100.0,
            "test_coverage_percent": 100.0,
            "coverage_percent": 100.0,
        },
        "run": {
            "id": 1,
            "vector_source": "native_sat_atpg",
            "vector_count": 1,
            "atpg_generation_seconds": 0.0,
            "fault_simulation_seconds": 0.0,
            "total_sim_seconds": 0.0,
            "coverage": 100.0,
            "atpg_terminal_reason": "COMPLETE",
            "atpg_rounds": 1,
            "atpg_sat": 1,
            "atpg_unsat": 0,
            "atpg_timeout": 0,
            "atpg_unknown": 0,
            "atpg_rejected_candidates": 0,
            "atpg_generated_vectors": 1,
            "atpg_accepted_vectors": 1,
        },
        "per_node": [],
        "reason_summary": {},
        "undetected_faults": [],
    }


def test_validate_report_resolves_schema_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_validate_report` must find the schema relative to the package, not cwd.
    A cwd-relative Path("schemas/...") crashed every coverage-producing command
    (sim/intest/extest/project) run from outside the repo root -- AFTER the ATPG
    had already completed (found in the e2e bug hunt)."""
    from faultflow.reporter.coverage import _validate_report

    monkeypatch.chdir(tmp_path)  # a dir with no schemas/ subtree
    _validate_report(_minimal_valid_report())  # must not raise


def test_validate_report_accepts_random_only_terminal_reason() -> None:
    """Found via a real bug: the schema's atpg_terminal_reason enum was never
    updated when the RANDOM_ONLY terminal mode (faultflow/runner/progressive_atpg.py
    and faultflow/scan/detection_pipeline.py, cfg.atpg.random_only) was added, so
    EVERY random_only=true run crashed at the final report-writing step -- after
    the campaign itself had already completed successfully. Caught mid-sweep: 15
    designs in a random-only comparison batch all failed with the same
    jsonschema.ValidationError before this was added."""
    from faultflow.reporter.coverage import _validate_report

    report = _minimal_valid_report()
    report["run"]["atpg_terminal_reason"] = "RANDOM_ONLY"
    _validate_report(report)  # must not raise


@pytest.mark.parametrize("sat_outcome", [None, "timeout"])
def test_undetected_reason_reports_blackbox_unresolved(sat_outcome: str | None) -> None:
    """blackbox_unresolved outranks a recorded SAT outcome: a fault can time out
    in one round and be proven UNSAT (then classified) in a later one, and the
    stale outcome row stays behind."""
    from faultflow.reporter.coverage import _undetected_reason

    assert (
        _undetected_reason(False, False, False, sat_outcome, blackbox_unresolved=True)
        == "blackbox_unresolved"
    )


def test_validate_report_accepts_a_blackbox_unresolved_fault() -> None:
    from faultflow.reporter.coverage import _validate_report

    report = _minimal_valid_report()
    report["summary"].update(detected=0, undetected=1, blackbox_unresolved=1)
    for key in ("fault_coverage_percent", "test_coverage_percent", "coverage_percent"):
        report["summary"][key] = 0.0
    report["undetected_faults"] = [
        {
            "id": 1,
            "net_id": 6,
            "net_name": "dout",
            "fault_type": "sa0",
            "fault_site_key": "net:6:stem",
            "protocol_unresolved": False,
            "compression_unresolved": False,
            "compaction_unresolved": False,
            "blackbox_unresolved": True,
            "reason": "blackbox_unresolved",
        }
    ]
    _validate_report(report)  # must not raise


def test_validate_report_rejects_malformed_from_any_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.reporter.coverage import CoverageError, _validate_report

    monkeypatch.chdir(tmp_path)
    bad = _minimal_valid_report()
    bad["summary"]["denominator"] = 0  # schema minimum is 1 (Policy 3)

    with pytest.raises((jsonschema.exceptions.ValidationError, CoverageError)):
        _validate_report(bad)
