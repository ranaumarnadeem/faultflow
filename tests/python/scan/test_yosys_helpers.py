"""`faultflow/scan/yosys.py` direct unit tests.

`run_scan_techmap_json` (JSON-output twin of `run_scan_techmap`, used by
`runner.py`'s scan-techmap-for-equivalence-sim path) had zero test coverage --
only the Verilog-output sibling was exercised anywhere in the suite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan import ScanError, stitch_scan_json, write_scan_techmap
from faultflow.scan.yosys import run_scan_techmap_json

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


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
                "netnames": {},
            }
        }
    }


def _write_json(path: Path, data: dict[str, object]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_run_scan_techmap_json_requires_yosys_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faultflow.scan.yosys as yosys_mod

    monkeypatch.setattr(yosys_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ScanError, match="requires yosys on PATH"):
        run_scan_techmap_json(
            generic_json=tmp_path / "missing.json",
            techmap_verilog=tmp_path / "missing.v",
            output_json=tmp_path / "out.json",
            log_path=tmp_path / "log.txt",
            script_path=tmp_path / "script.ys",
        )


def test_run_scan_techmap_json_requires_generic_json(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    with pytest.raises(ScanError, match="missing generic scanned JSON"):
        run_scan_techmap_json(
            generic_json=tmp_path / "missing.json",
            techmap_verilog=tmp_path / "missing.v",
            output_json=tmp_path / "out.json",
            log_path=tmp_path / "log.txt",
            script_path=tmp_path / "script.ys",
        )


def test_run_scan_techmap_json_requires_techmap_verilog(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    generic = _write_json(tmp_path / "generic.json", {"modules": {}})

    with pytest.raises(ScanError, match="missing scan techmap file"):
        run_scan_techmap_json(
            generic_json=generic,
            techmap_verilog=tmp_path / "missing.v",
            output_json=tmp_path / "out.json",
            log_path=tmp_path / "log.txt",
            script_path=tmp_path / "script.ys",
        )


@pytest.mark.integration
def test_run_scan_techmap_json_produces_sky130_cell_json(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    generic = tmp_path / "tiny_dff_scan_generic.json"
    techmap = tmp_path / "faultflow_scanff_map.v"
    out_json = tmp_path / "tiny_dff_scan.json"
    stitch_scan_json(source, CELL_MAP, "tiny_dff", generic)
    write_scan_techmap(techmap)

    result = run_scan_techmap_json(
        generic_json=generic,
        techmap_verilog=techmap,
        output_json=out_json,
        log_path=tmp_path / "yosys_scan_json.log",
        script_path=tmp_path / "yosys_scan_json.ys",
    )

    assert result == out_json
    data = json.loads(out_json.read_text(encoding="utf-8"))
    types = {
        cell["type"]
        for module in data["modules"].values()
        for cell in module["cells"].values()
    }
    assert "sky130_fd_sc_hd__sdfxtp_1" in types
    assert not any("scanff_faultflow" in t for t in types)
