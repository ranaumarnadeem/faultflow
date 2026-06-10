from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.scan import (
    ScanError,
    YOSYS_SCAN_CELL_TYPE,
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
    assert result.scan_inputs == ["scan_in_0"]
    assert result.scan_outputs == ["scan_out_0"]
    assert cell["type"] == YOSYS_SCAN_CELL_TYPE
    assert cell["connections"]["SI"] == module["ports"]["scan_in_0"]["bits"]
    assert cell["connections"]["SE"] == module["ports"]["scan_en"]["bits"]
    assert module["ports"]["scan_out_0"]["bits"] == [4]


def test_stitch_scan_json_rejects_reset_ff(tmp_path: Path) -> None:
    source = ROOT / "tests/cpp/fixtures/tiny_dffsr.json"

    with pytest.raises(ScanError, match="reset/set FF"):
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
    assert ".SCD(SI)" in text
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
    assert cycles[1]["scan_in_0"] is True
    assert cycles[5]["scan_en"] is False


def test_scan_cli_writes_manifest_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--skip-techmap"]) == 0

    scan_dir = tmp_path / "output/tiny_dff/scan"
    assert (scan_dir / "tiny_dff_scan_generic.json").exists()
    assert (scan_dir / "faultflow_scanff_map.v").exists()
    manifest = json.loads((scan_dir / "scan_manifest.json").read_text())
    assert manifest["cell_count"] == 1
    assert manifest["sky130_verilog"] is None


@pytest.mark.integration
def test_yosys_scan_techmap_produces_sky130_cell(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    generic = tmp_path / "tiny_dff_scan_generic.json"
    techmap = tmp_path / "faultflow_scanff_map.v"
    out_v = tmp_path / "tiny_dff_scan_sky130.v"
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
