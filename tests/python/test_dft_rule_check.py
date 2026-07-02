from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.rule_check.rules import rules_scan, run_rule_check

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

AND2 = "sky130_fd_sc_hd__and2_1"
INV = "sky130_fd_sc_hd__inv_1"
BUF = "sky130_fd_sc_hd__buf_1"
DFF = "sky130_fd_sc_hd__dfxtp_1"


def _cell(ctype: str, conns: dict[str, list[int]]) -> dict[str, object]:
    return {
        "hide_name": 0,
        "type": ctype,
        "parameters": {},
        "attributes": {},
        "connections": conns,
    }


def _module(
    top: str,
    ports: dict[str, dict[str, object]],
    cells: dict[str, object],
) -> dict[str, object]:
    return {
        "modules": {
            top: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {},
            }
        }
    }


def _port(direction: str, bit: int) -> dict[str, object]:
    return {"direction": direction, "bits": [bit]}


def _write(tmp_path: Path, name: str, design: dict[str, object]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(design, indent=2) + "\n", encoding="utf-8")
    return path


def _rule_ids(report: object) -> set[str]:
    return {v.rule_id for v in report.violations}  # type: ignore[attr-defined]


def test_clean_combinational_passes(tmp_path: Path) -> None:
    design = _module(
        "comb",
        {"A": _port("input", 2), "B": _port("input", 3), "Y": _port("output", 4)},
        {"u0": _cell(AND2, {"A": [2], "B": [3], "X": [4]})},
    )
    report = run_rule_check(_write(tmp_path, "comb", design), CELL_MAP, "comb")
    assert report.passed()
    assert not report.violations


def test_single_clock_dff_from_pi_passes(tmp_path: Path) -> None:
    design = _module(
        "ff",
        {"CLK": _port("input", 2), "D": _port("input", 3), "Q": _port("output", 4)},
        {"u0": _cell(DFF, {"CLK": [2], "D": [3], "Q": [4]})},
    )
    report = run_rule_check(_write(tmp_path, "ff", design), CELL_MAP, "ff")
    assert report.passed()


def test_uncontrollable_clock_flagged(tmp_path: Path) -> None:
    # CLK pin driven by an AND2 output (net 5), not a PI / buffer tree.
    design = _module(
        "gated",
        {
            "A": _port("input", 2),
            "B": _port("input", 3),
            "D": _port("input", 4),
            "Q": _port("output", 7),
        },
        {
            "u_and": _cell(AND2, {"A": [2], "B": [3], "X": [5]}),
            "u_ff": _cell(DFF, {"CLK": [5], "D": [4], "Q": [7]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "gated", design), CELL_MAP, "gated")
    assert "CLK001" in _rule_ids(report)
    assert not report.passed()


def test_clock_through_buffer_is_controllable(tmp_path: Path) -> None:
    # CLK reaches the FF through a buffer chain rooted at a PI -> controllable.
    design = _module(
        "buftree",
        {"CLK": _port("input", 2), "D": _port("input", 3), "Q": _port("output", 5)},
        {
            "u_buf": _cell(BUF, {"A": [2], "X": [4]}),
            "u_ff": _cell(DFF, {"CLK": [4], "D": [3], "Q": [5]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "buftree", design), CELL_MAP, "buftree")
    assert "CLK001" not in _rule_ids(report)


def test_clock_as_data_flagged(tmp_path: Path) -> None:
    # CLK (PI net 2) feeds an FF clock pin AND an AND2 data pin.
    design = _module(
        "clkdata",
        {
            "CLK": _port("input", 2),
            "D": _port("input", 3),
            "Q": _port("output", 4),
            "Y": _port("output", 6),
        },
        {
            "u_ff": _cell(DFF, {"CLK": [2], "D": [3], "Q": [4]}),
            "u_and": _cell(AND2, {"A": [2], "B": [3], "X": [6]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "clkdata", design), CELL_MAP, "clkdata")
    assert "CLK002" in _rule_ids(report)
    # clock-as-data is a warning, not a hard error -> still passes by default.
    assert report.passed()
    assert not report.passed(strict=True)


def test_multiple_clock_domains_flagged(tmp_path: Path) -> None:
    design = _module(
        "multiclk",
        {
            "CLK1": _port("input", 2),
            "CLK2": _port("input", 3),
            "D1": _port("input", 4),
            "D2": _port("input", 5),
            "Q1": _port("output", 6),
            "Q2": _port("output", 7),
        },
        {
            "u_ff1": _cell(DFF, {"CLK": [2], "D": [4], "Q": [6]}),
            "u_ff2": _cell(DFF, {"CLK": [3], "D": [5], "Q": [7]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "multiclk", design), CELL_MAP, "multiclk")
    assert "CLK003" in _rule_ids(report)
    # CLK003 is now INFO (multi-clock is supported for stuck-at + scan).
    assert report.passed()
    clk003 = next(v for v in report.violations if v.rule_id == "CLK003")
    from faultflow.rule_check.model import Severity

    assert clk003.severity is Severity.INFO
    assert "2 clock domains" in clk003.message


def test_combinational_feedback_flagged(tmp_path: Path) -> None:
    # net4 = AND(A, net5); net5 = INV(net4) -> combinational loop 4 -> 5 -> 4.
    design = _module(
        "loop",
        {"A": _port("input", 2), "Y": _port("output", 5)},
        {
            "u_and": _cell(AND2, {"A": [2], "B": [5], "X": [4]}),
            "u_inv": _cell(INV, {"A": [4], "Y": [5]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "loop", design), CELL_MAP, "loop")
    assert "STRUCT001" in _rule_ids(report)
    assert not report.passed()


def test_multi_driver_flagged(tmp_path: Path) -> None:
    design = _module(
        "multidrv",
        {"A": _port("input", 2), "B": _port("input", 3), "Y": _port("output", 4)},
        {
            "u0": _cell(BUF, {"A": [2], "X": [4]}),
            "u1": _cell(BUF, {"A": [3], "X": [4]}),
        },
    )
    report = run_rule_check(_write(tmp_path, "multidrv", design), CELL_MAP, "multidrv")
    assert "NET001" in _rule_ids(report)
    assert not report.passed()


def test_scan_rules_surface_manifest_findings() -> None:
    # A malformed manifest (no generic_json) is itself a SCAN001 error; ineligible
    # FFs surface as SCAN010 warnings.
    manifest = {
        "ineligible_ffs": [
            {"instance": "u_bad", "cell_type": "DFRTP", "reason": "has_async_reset"}
        ]
    }
    violations = rules_scan(manifest)
    ids = {v.rule_id for v in violations}
    assert "SCAN001" in ids  # structural check failed on the malformed manifest
    assert "SCAN010" in ids  # non-scannable FF reported


# --- CLI exit-code behaviour -------------------------------------------------


def _write_config(tmp_path: Path, netlist: Path) -> Path:
    cfg = tmp_path / "config.ofs"
    cfg.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_rule_check_blocks_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    design = _module(
        "gated",
        {
            "A": _port("input", 2),
            "B": _port("input", 3),
            "D": _port("input", 4),
            "Q": _port("output", 7),
        },
        {
            "u_and": _cell(AND2, {"A": [2], "B": [3], "X": [5]}),
            "u_ff": _cell(DFF, {"CLK": [5], "D": [4], "Q": [7]}),
        },
    )
    netlist = _write(tmp_path, "gated", design)
    cfg = _write_config(tmp_path, netlist)

    # Default: an ERROR violation makes rule_check a blocking gate (exit 1).
    assert main(["rule_check", "--top", "gated", "-c", str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "CLK001" in out
    assert "FAIL" in out

    # --advisory downgrades to report-only (exit 0).
    assert main(["rule_check", "--top", "gated", "-c", str(cfg), "--advisory"]) == 0


def test_cli_rule_check_passes_clean_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    design = _module(
        "comb",
        {"A": _port("input", 2), "B": _port("input", 3), "Y": _port("output", 4)},
        {"u0": _cell(AND2, {"A": [2], "B": [3], "X": [4]})},
    )
    netlist = _write(tmp_path, "comb", design)
    cfg = _write_config(tmp_path, netlist)
    assert main(["rule_check", "--top", "comb", "-c", str(cfg)]) == 0
    assert (tmp_path / "output" / "comb" / "rule_check.rpt").exists()


# --- rule_check.json schema validation ---------------------------------------


def test_rule_check_json_validates_against_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jsonschema = pytest.importorskip("jsonschema")

    monkeypatch.chdir(tmp_path)
    design = _module(
        "gated",
        {
            "A": _port("input", 2),
            "B": _port("input", 3),
            "D": _port("input", 4),
            "Q": _port("output", 7),
        },
        {
            "u_and": _cell(AND2, {"A": [2], "B": [3], "X": [5]}),
            "u_ff": _cell(DFF, {"CLK": [5], "D": [4], "Q": [7]}),
        },
    )
    netlist = _write(tmp_path, "gated", design)
    cfg = _write_config(tmp_path, netlist)
    assert main(["rule_check", "--top", "gated", "-c", str(cfg)]) == 1

    json_path = tmp_path / "output" / "gated" / "rule_check.json"
    assert json_path.exists()
    report = json.loads(json_path.read_text(encoding="utf-8"))

    schema_path = ROOT / "schemas" / "rule_check.schema.json"
    assert schema_path.exists(), f"missing schema: {schema_path}"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(report, schema)

    assert report["status"] == "FAIL"
    assert any(v["rule_id"] == "CLK001" for v in report["violations"])


def test_rule_check_schema_rejects_malformed_report() -> None:
    jsonschema = pytest.importorskip("jsonschema")

    schema_path = ROOT / "schemas" / "rule_check.schema.json"
    assert schema_path.exists(), f"missing schema: {schema_path}"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    malformed = {
        "version": 1,
        "top": "gated",
        # "status" missing entirely -- required field
        "summary": {"errors": 1, "warnings": 0, "info": 0},
        "violations": [
            {
                "rule_id": "CLK001",
                # "severity" missing; also has a bogus extra key below
                "title": "Uncontrollable clock",
                "message": "clock net driven by combinational logic",
                "unexpected_extra_field": True,
            }
        ],
    }

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(malformed, schema)


# --------------------------------------------------------------------------- #
# _validate_report_shape: the manual fallback used when jsonschema is not     #
# installed (a genuinely optional dependency). Exercised directly rather than #
# by uninstalling jsonschema, since it's a plain, independently testable      #
# function.                                                                    #
# --------------------------------------------------------------------------- #


def _well_formed_report() -> dict[str, object]:
    return {
        "version": 1,
        "top": "gated",
        "status": "PASS",
        "summary": {"errors": 0, "warnings": 0, "info": 0},
        "violations": [],
    }


def test_validate_report_shape_rejects_missing_top_level_key() -> None:
    from faultflow.rule_check.report import RuleCheckReportError, _validate_report_shape

    report = _well_formed_report()
    del report["status"]

    with pytest.raises(RuleCheckReportError, match="missing keys"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_missing_summary_key() -> None:
    from faultflow.rule_check.report import RuleCheckReportError, _validate_report_shape

    report = _well_formed_report()
    del report["summary"]["warnings"]  # type: ignore[attr-defined]

    with pytest.raises(RuleCheckReportError, match="summary missing keys"):
        _validate_report_shape(report)


def test_validate_report_shape_rejects_non_list_violations() -> None:
    from faultflow.rule_check.report import RuleCheckReportError, _validate_report_shape

    report = _well_formed_report()
    report["violations"] = {}

    with pytest.raises(RuleCheckReportError, match="violations must be an array"):
        _validate_report_shape(report)


def test_validate_report_shape_accepts_well_formed_report() -> None:
    from faultflow.rule_check.report import _validate_report_shape

    _validate_report_shape(_well_formed_report())  # must not raise
