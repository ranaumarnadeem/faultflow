"""Real, CLI-driven end-to-end coverage for scan compression AND compaction
enabled SIMULTANEOUSLY in one campaign: `ff.py scan` -> `scan-check` ->
`scan-compress` -> `scan-compact` -> `rule_check` -> `sim --scan`, on a real
Yosys-synthesizable design, through the actual `main()` entry point.

Each side was built and verified independently (test_scan_compress_cli.py,
test_scan_compact_cli.py); this proves they COMPOSE -- the round loop's two
independent post-hoc filters (_check_compression_satisfiable,
_check_compaction_distinguishable) both active on the same campaign, the
manifest's disjoint "compression"/"compaction" sections both surviving the
scan-compress -> scan-compact sequence intact, and a real ATPG run producing
a well-formed coverage report with both compression_unresolved and
compaction_unresolved present."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.rule_check.model import Severity

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"


def _dfxtp_cell(clk: int, d: int, q: int) -> dict:
    return {
        "hide_name": 0,
        "type": "sky130_fd_sc_hd__dfxtp_1",
        "parameters": {},
        "attributes": {},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": [clk], "D": [d], "Q": [q]},
    }


def _four_independent_d_chain_json() -> dict[str, object]:
    """4 independent plain DFFs, each with its OWN D input -- `ff.py scan`
    stitches each into its own 1-FF chain ([scan] chains = 4), giving
    compaction (channels < 4) something meaningful to fold and compression
    (channels = 8) a real multi-chain decompressor to drive."""
    return {
        "modules": {
            "core_top": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D0": {"direction": "input", "bits": [3]},
                    "D1": {"direction": "input", "bits": [4]},
                    "D2": {"direction": "input", "bits": [5]},
                    "D3": {"direction": "input", "bits": [6]},
                    "Q0": {"direction": "output", "bits": [7]},
                    "Q1": {"direction": "output", "bits": [8]},
                    "Q2": {"direction": "output", "bits": [9]},
                    "Q3": {"direction": "output", "bits": [10]},
                },
                "cells": {
                    "u0": _dfxtp_cell(2, 3, 7),
                    "u1": _dfxtp_cell(2, 4, 8),
                    "u2": _dfxtp_cell(2, 5, 9),
                    "u3": _dfxtp_cell(2, 6, 10),
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D0": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "D1": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "D2": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "D3": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "Q0": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "Q1": {"hide_name": 0, "bits": [8], "attributes": {}},
                    "Q2": {"hide_name": 0, "bits": [9], "attributes": {}},
                    "Q3": {"hide_name": 0, "bits": [10], "attributes": {}},
                },
            }
        }
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_config(path: Path, netlist: Path) -> Path:
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}
liberty = {LIBERTY}

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
output = missing.test
max_rounds = 50

[scan]
chains = 4

[compression]
enabled = true
channels = 8

[compaction]
enabled = true
channels = 2
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_scan_compress_and_compact_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "core_top.json", _four_independent_d_chain_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "core_top", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "core_top", "-c", str(cfg_path)]) == 0

    manifest_path = tmp_path / "output/core_top/.faultflow/manifests/scan_manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert before["latest_check"]["status"] == "PASS"

    assert main(["scan-compress", "--top", "core_top", "-c", str(cfg_path)]) == 0
    after_compress = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert (
        after_compress["compression"]["structural_check"]["status"] == "PASS"
    ), after_compress["compression"]["structural_check"]["errors"]

    assert main(["scan-compact", "--top", "core_top", "-c", str(cfg_path)]) == 0
    after_compact = json.loads(manifest_path.read_text(encoding="utf-8"))

    # generic_json/hash/latest_check must survive BOTH transforms untouched
    # -- ATPG keeps reasoning against the pre-compression/pre-compaction
    # netlist regardless of how many manufacturing-artifact transforms ran.
    assert after_compact["generic_json"] == before["generic_json"]
    assert after_compact["generic_json_hash"] == before["generic_json_hash"]
    assert after_compact["latest_check"] == before["latest_check"]

    # scan-compact must not have clobbered scan-compress's section, and
    # vice versa -- both sections present and PASSing simultaneously.
    compression = after_compact["compression"]
    compaction = after_compact["compaction"]
    assert compression["enabled"] is True
    assert compression["structural_check"]["status"] == "PASS", compression[
        "structural_check"
    ]["errors"]
    assert compaction["enabled"] is True
    assert compaction["structural_check"]["status"] == "PASS", compaction[
        "structural_check"
    ]["errors"]

    from faultflow.config import load_config
    from faultflow.runner.runner import Runner

    report = Runner(load_config(cfg_path, "core_top")).rule_check()
    comp_errors = [
        v
        for v in report.violations
        if v.rule_id in ("COMP001", "COMP002") and v.severity == Severity.ERROR
    ]
    assert comp_errors == []

    # Real ATPG, both post-hoc filters (_check_compression_satisfiable,
    # _check_compaction_distinguishable) simultaneously active.
    assert main(["sim", "--top", "core_top", "-c", str(cfg_path), "--scan"]) == 0

    coverage_json = (
        tmp_path / "output/core_top/.faultflow/intermediate/coverage_report.json"
    )
    report_data = json.loads(coverage_json.read_text(encoding="utf-8"))
    summary = report_data["summary"]
    assert "compression_unresolved" in summary
    assert "compaction_unresolved" in summary
    assert summary["denominator"] > 0
