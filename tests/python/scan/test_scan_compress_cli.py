"""Real, CLI-driven end-to-end coverage for scan compression: `ff.py scan` ->
`scan-check` -> `scan-compress` -> `rule_check`, on a real Yosys-synthesizable
design, through the actual `main()` entry point -- not a hand-assembled
manifest (see test_compression_end_to_end.py for the lower-level mechanism
proof, which predates this CLI wiring)."""

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


def _write_config(path: Path, netlist: Path, *, channels: int = 8) -> Path:
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

[compression]
enabled = true
channels = {channels}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.integration
def test_scan_compress_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    manifest_path = (
        tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json"
    )
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert before["latest_check"]["status"] == "PASS"

    assert main(["scan-compress", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    # generic_json/hash/latest_check are untouched -- ATPG must keep reasoning
    # against the pre-compression netlist, per the compression CLI-wiring plan.
    assert after["generic_json"] == before["generic_json"]
    assert after["generic_json_hash"] == before["generic_json_hash"]
    assert after["latest_check"] == before["latest_check"]

    compression = after["compression"]
    assert compression["enabled"] is True
    assert compression["num_channels"] == 8
    assert compression["scan_in_ports"] == ["scan_in"]
    assert compression["structural_check"]["status"] == "PASS", compression[
        "structural_check"
    ]["errors"]
    composed_json = Path(compression["composed_json"])
    assert composed_json.exists()
    composed = json.loads(composed_json.read_text(encoding="utf-8"))
    assert compression["composed_top"] in composed["modules"]

    from faultflow.config import load_config
    from faultflow.runner.runner import Runner

    report = Runner(load_config(cfg_path, "tiny_dff")).rule_check()
    comp_errors = [
        v for v in report.violations if v.rule_id == "COMP001" and v.severity == Severity.ERROR
    ]
    assert comp_errors == []


@pytest.mark.integration
def test_scan_compress_requires_compression_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    # Config with NO [compression] section at all -- enabled defaults False.
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
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "tiny_dff", "-c", str(cfg_path)]) == 0

    with pytest.raises(SystemExit) as exc:
        main(["scan-compress", "--top", "tiny_dff", "-c", str(cfg_path)])
    assert exc.value.code == 2


@pytest.mark.unit
def test_default_clock_port_resolves_single_declared_clock(tmp_path: Path) -> None:
    from faultflow.config import load_config
    from faultflow.runner.runner import Runner

    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)
    runner = Runner(load_config(cfg_path, "tiny_dff"))
    manifest = {"top": "tiny_dff", "clock_nets": [2]}

    assert runner._default_clock_port(manifest, source) == "CLK"


@pytest.mark.unit
def test_default_clock_port_rejects_zero_declared_clocks(tmp_path: Path) -> None:
    from faultflow.config import load_config
    from faultflow.runner.runner import Runner, RunnerError

    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)
    runner = Runner(load_config(cfg_path, "tiny_dff"))
    manifest = {"top": "tiny_dff", "clock_nets": []}

    # manifest_clock_net_ids itself rejects an empty/absent clock declaration
    # before _default_clock_port's own ambiguity check ever runs -- still a
    # clean RunnerError either way.
    with pytest.raises(RunnerError, match="clock_nets"):
        runner._default_clock_port(manifest, source)


@pytest.mark.unit
def test_default_clock_port_rejects_multiple_declared_clocks(tmp_path: Path) -> None:
    from faultflow.config import load_config
    from faultflow.runner.runner import Runner, RunnerError

    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg_path = _write_config(tmp_path / "config.ofs", source)
    runner = Runner(load_config(cfg_path, "tiny_dff"))
    manifest = {"top": "tiny_dff", "clock_nets": [2, 99]}

    with pytest.raises(RunnerError, match=r"\[compression\] clock must be set"):
        runner._default_clock_port(manifest, source)


@pytest.mark.integration
def test_scan_compress_requires_scan_check_pass_first(
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
        main(["scan-compress", "--top", "tiny_dff", "-c", str(cfg_path)])
    assert exc.value.code == 2
