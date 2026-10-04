"""Real, CLI-driven end-to-end coverage for scan compaction: `ff.py scan` ->
`scan-check` -> `scan-compact` -> `rule_check`, on a real Yosys-synthesizable
design, through the actual `main()` entry point -- mirrors
test_scan_compress_cli.py's structure for the output-side compactor."""

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


def _tiny_dff_json() -> dict[str, object]:
    return {
        "modules": {
            "tiny_dff": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "Q": [4]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        }
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_config(path: Path, netlist: Path, *, channels: int = 1) -> Path:
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

[compaction]
enabled = true
channels = {channels}
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_scan_compact_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    manifest_path = tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert before["latest_check"]["status"] == "PASS"

    assert main(["scan-compact", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    # generic_json/hash/latest_check are untouched -- ATPG must keep reasoning
    # against the pre-compaction netlist, mirroring the compression rule.
    assert after["generic_json"] == before["generic_json"]
    assert after["generic_json_hash"] == before["generic_json_hash"]
    assert after["latest_check"] == before["latest_check"]

    compaction = after["compaction"]
    assert compaction["enabled"] is True
    assert compaction["num_outputs"] == 1
    assert compaction["scan_out_ports"] == ["scan_out"]
    assert compaction["structural_check"]["status"] == "PASS", compaction[
        "structural_check"
    ]["errors"]
    composed_json = Path(compaction["composed_json"])
    assert composed_json.exists()
    composed = json.loads(composed_json.read_text(encoding="utf-8"))
    assert compaction["composed_top"] in composed["modules"]

    from faultflow.config import load_config
    from faultflow.runner.runner import Runner

    report = Runner(load_config(cfg_path, "tiny_dff")).rule_check()
    comp_errors = [
        v
        for v in report.violations
        if v.rule_id == "COMP002" and v.severity == Severity.ERROR
    ]
    assert comp_errors == []


@pytest.mark.integration
def test_scan_compact_requires_compaction_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    # Config with NO [compaction] section at all -- enabled defaults False.
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {source}
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
""".strip() + "\n",
        encoding="utf-8",
    )

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    with pytest.raises(SystemExit) as exc:
        main(["scan-compact", "--top", "tiny_dff", "-c", str(cfg_path)])
    assert exc.value.code == 2


@pytest.mark.integration
def test_scan_compact_requires_scan_check_pass_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg_path), "--no-techmap"]) == 0
    # Note: scan-check is deliberately skipped here.

    with pytest.raises(SystemExit) as exc:
        main(["scan-compact", "--top", "tiny_dff", "-c", str(cfg_path)])
    assert exc.value.code == 2
