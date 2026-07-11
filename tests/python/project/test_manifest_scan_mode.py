"""`load_project` must parse and validate interconnect.mode="scan" manifests.

Scan-model EXTEST composes its assembly netlist at run time from a glue RTL + each
block's frozen JSON (see `faultflow/project/assemble.py`), so it needs different
manifest fields than the pre-existing comb-mode (buffer-model) shape: a glue RTL
path, the SoC-level wrapper port names, a clock port, and -- per block -- the
instance name used in that glue RTL (`soc_instance`). See `manifest.py`'s module
docstring for the full schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.manifest import ProjectError, load_project


def _base_manifest(root: Path) -> dict[str, Any]:
    (root / "base.ofs").write_text("[design]\n", encoding="utf-8")
    for name in ("blkA", "blkB"):
        (root / f"{name}.json").write_text("{}", encoding="utf-8")
        (root / f"{name}_manifest.json").write_text("{}", encoding="utf-8")
    (root / "glue.v").write_text("module soc_top(); endmodule\n", encoding="utf-8")
    return {
        "schema": "faultflow_project_v1",
        "name": "socN",
        "base_config": str(root / "base.ofs"),
        "blocks": [
            {
                "name": "blkA",
                "top": "alu_acc",
                "soc_instance": "u_a",
                "generic_json": str(root / "blkA.json"),
                "scan_manifest": str(root / "blkA_manifest.json"),
            },
            {
                "name": "blkB",
                "top": "ctr_fsm",
                "soc_instance": "u_b",
                "generic_json": str(root / "blkB.json"),
                "scan_manifest": str(root / "blkB_manifest.json"),
            },
        ],
        "interconnect": {
            "assembly_top": "soc_top",
            "mode": "scan",
            "soc_rtl": str(root / "glue.v"),
            "soc_wbr_si": "soc_wbr_si",
            "soc_wbr_so": "soc_wbr_so",
            "soc_wbr_se": "soc_wbr_se",
            "clock_port": "clk",
        },
    }


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "project.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_scan_mode_parses_soc_rtl_and_wrapper_ports(tmp_path: Path) -> None:
    manifest_path = _write(tmp_path, _base_manifest(tmp_path))
    project = load_project(manifest_path)

    ic = project.interconnect
    assert ic.mode == "scan"
    assert ic.soc_rtl == tmp_path / "glue.v"
    assert ic.soc_wbr_si == "soc_wbr_si"
    assert ic.soc_wbr_so == "soc_wbr_so"
    assert ic.soc_wbr_se == "soc_wbr_se"
    assert ic.clock_port == "clk"
    assert ic.assembly_netlist is None
    assert ic.blackbox_instances == ()

    by_name = {b.name: b for b in project.blocks}
    assert by_name["blkA"].soc_instance == "u_a"
    assert by_name["blkB"].soc_instance == "u_b"


def test_scan_mode_requires_soc_instance_per_block(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    del data["blocks"][0]["soc_instance"]
    manifest_path = _write(tmp_path, data)

    with pytest.raises(ProjectError, match="soc_instance"):
        load_project(manifest_path)


def test_scan_mode_rejects_duplicate_soc_instance(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    data["blocks"][1]["soc_instance"] = "u_a"
    manifest_path = _write(tmp_path, data)

    with pytest.raises(ProjectError, match="duplicate soc_instance"):
        load_project(manifest_path)


def test_scan_mode_rejects_comb_only_fields(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    data["interconnect"]["assembly_netlist"] = str(tmp_path / "asm.json")
    manifest_path = _write(tmp_path, data)

    with pytest.raises(ProjectError, match="assembly_netlist.*comb-mode-only"):
        load_project(manifest_path)


def test_scan_mode_requires_wrapper_port_fields(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    del data["interconnect"]["soc_wbr_se"]
    manifest_path = _write(tmp_path, data)

    with pytest.raises(ProjectError, match="soc_wbr_se"):
        load_project(manifest_path)


def test_comb_mode_rejects_scan_only_fields(tmp_path: Path) -> None:
    """Back-compat guard the other direction: a comb-mode manifest must reject the
    scan-only fields rather than silently ignoring them."""
    (tmp_path / "base.ofs").write_text("[design]\n", encoding="utf-8")
    (tmp_path / "blkA.json").write_text("{}", encoding="utf-8")
    (tmp_path / "blkA_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "asm.json").write_text("{}", encoding="utf-8")
    data = {
        "schema": "faultflow_project_v1",
        "name": "socN",
        "base_config": str(tmp_path / "base.ofs"),
        "blocks": [
            {
                "name": "blkA",
                "top": "blkA",
                "generic_json": str(tmp_path / "blkA.json"),
                "scan_manifest": str(tmp_path / "blkA_manifest.json"),
            }
        ],
        "interconnect": {
            "assembly_top": "soc_top",
            "assembly_netlist": str(tmp_path / "asm.json"),
            "soc_rtl": str(tmp_path / "glue.v"),
        },
    }
    manifest_path = _write(tmp_path, data)

    with pytest.raises(ProjectError, match="soc_rtl.*scan-mode-only"):
        load_project(manifest_path)
