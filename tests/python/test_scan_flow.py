from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.cli import main
from faultflow.scan import (
    ScanError,
    YOSYS_SCAN_CELL_TYPE,
    balanced_chain_lengths,
    render_scan_techmap,
    run_scan_techmap,
    scan_shift_capture_shiftout_cycles,
    stitch_scan_json,
    write_scan_techmap,
)

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/osu/osu035.json"


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
                        "type": "DFFPOSX1",
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


def _write_config(path: Path, netlist: Path) -> Path:
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

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
    return path


def test_stitch_scan_json_replaces_plain_ff(tmp_path: Path) -> None:
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    output = tmp_path / "tiny_dff_scan_generic.json"

    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", output)

    data = json.loads(output.read_text(encoding="utf-8"))
    module = data["modules"]["tiny_dff"]
    cell = module["cells"]["u0"]
    assert result.cell_count == 1
    assert result.scan_inputs == ["scan_in"]
    assert result.scan_outputs == ["scan_out"]
    assert cell["type"] == YOSYS_SCAN_CELL_TYPE
    assert cell["connections"]["SDI"] == module["ports"]["scan_in"]["bits"]
    assert cell["connections"]["SE"] == module["ports"]["scan_en"]["bits"]
    assert module["ports"]["scan_out"]["bits"] == [4]


def test_stitch_scan_json_builds_balanced_multi_chains(tmp_path: Path) -> None:
    data = _tiny_dff_json()
    modules = data["modules"]
    assert isinstance(modules, dict)
    tiny = modules["tiny_dff"]
    assert isinstance(tiny, dict)
    cells_obj = tiny["cells"]
    netnames_obj = tiny["netnames"]
    assert isinstance(cells_obj, dict)
    assert isinstance(netnames_obj, dict)
    cells: dict[str, Any] = cells_obj
    netnames: dict[str, Any] = netnames_obj
    for index in range(1, 5):
        d_net = 10 + index
        q_net = 20 + index
        cells[f"u{index}"] = {
            "hide_name": 0,
            "type": "DFFPOSX1",
            "parameters": {},
            "attributes": {},
            "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
            "connections": {"CLK": [2], "D": [d_net], "Q": [q_net]},
        }
        netnames[f"D{index}"] = {"hide_name": 0, "bits": [d_net], "attributes": {}}
        netnames[f"Q{index}"] = {"hide_name": 0, "bits": [q_net], "attributes": {}}
    source = _write_json(tmp_path / "many_dff.json", data)
    output = tmp_path / "many_dff_scan_generic.json"

    result = stitch_scan_json(
        source,
        CELL_MAP,
        "tiny_dff",
        output,
        scan_chains=2,
        max_chain_length=3,
        scan_in_base="scan_si",
        scan_out_base="scan_so",
        scan_enable="test_se",
    )

    assert balanced_chain_lengths(5, 2) == [3, 2]
    assert result.scan_inputs == ["scan_si_0", "scan_si_1"]
    assert result.scan_outputs == ["scan_so_0", "scan_so_1"]
    assert [chain.length for chain in result.chains] == [3, 2]
    assert result.scan_enable == "test_se"


def test_stitch_scan_json_rejects_reset_ff(tmp_path: Path) -> None:
    source = ROOT / "tests/cpp/fixtures/tiny_dffsr.json"

    with pytest.raises(ScanError, match="has_async_reset_set"):
        stitch_scan_json(
            source,
            CELL_MAP,
            "tiny_dffsr",
            tmp_path / "tiny_dffsr_scan_generic.json",
        )


def test_render_scan_techmap_targets_sky130_scan_cell() -> None:
    text = render_scan_techmap()
    celltype = YOSYS_SCAN_CELL_TYPE.replace("\\", "\\\\")

    assert f'techmap_celltype = "{celltype}"' in text
    assert "sky130_fd_sc_hd__sdfxtp_1 _TECHMAP_REPLACE_" in text
    assert ".SCD(SDI)" in text
    assert ".SCE(SE)" in text


def test_scan_protocol_shift_capture_shiftout_cycles() -> None:
    cycles = scan_shift_capture_shiftout_cycles(
        [True, False],
        {"D": True},
        shift_out_length=2,
    )

    assert len(cycles) == 10
    assert cycles[0]["CLK"] is False
    assert cycles[1]["CLK"] is True
    assert cycles[1]["scan_en"] is True
    assert cycles[1]["scan_in"] is True
    assert cycles[5]["scan_en"] is False


def test_scan_cli_writes_manifest_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0

    out = tmp_path / "output/tiny_dff"
    assert (out / "tiny_dff_scan.json").exists()
    workspace = out / ".faultflow"
    assert (workspace / "generated_scripts" / "faultflow_scanff_map.v").exists()
    manifest = json.loads((workspace / "manifests" / "scan_manifest.json").read_text())
    assert manifest["cell_count"] == 1
    assert (workspace / "manifests" / "scan_chains.txt").exists()
    assert (out / "scan.rpt").exists()
    assert manifest["sky130_verilog"] is None


def test_scan_cli_dry_run_writes_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert (
        main(
            [
                "scan",
                "--top",
                "tiny_dff",
                "-c",
                str(cfg),
                "--dry-run",
                "--scan-chains",
                "1",
            ]
        )
        == 0
    )

    text = capsys.readouterr().out
    assert "scan dry-run top=tiny_dff" in text
    assert "eligible_ffs=1" in text
    assert not (tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json").exists()


def test_scan_check_writes_fail_result_on_structural_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0
    generic = tmp_path / "output/tiny_dff/tiny_dff_scan.json"
    generic.write_text(generic.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["scan-check", "--top", "tiny_dff", "-c", str(cfg)])

    assert exc.value.code == 2
    manifest = json.loads(
        (tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json").read_text()
    )
    assert manifest["latest_check"]["status"] == "FAIL"
    assert "generic JSON hash" in manifest["latest_check"]["errors"][0]


@pytest.mark.integration
def test_yosys_scan_techmap_produces_sky130_cell(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    generic = tmp_path / "tiny_dff_scan_generic.json"
    techmap = tmp_path / "faultflow_scanff_map.v"
    out_v = tmp_path / "tiny_dff_scan.v"
    stitch_scan_json(source, CELL_MAP, "tiny_dff", generic)
    write_scan_techmap(techmap)

    run_scan_techmap(
        generic_json=generic,
        techmap_verilog=techmap,
        output_verilog=out_v,
        top="tiny_dff",
        log_path=tmp_path / "yosys_scan.log",
        script_path=tmp_path / "yosys_scan.ys",
    )

    text = out_v.read_text(encoding="utf-8")
    assert "sky130_fd_sc_hd__sdfxtp_1" in text
    assert "scanff_faultflow" not in text
