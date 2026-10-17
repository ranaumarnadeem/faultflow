"""The scan techmap maps FaultFlow's generic scan flops and the gates stitching adds
onto the design's own library, by role (faultflow.pdk.roles): Sky130 HD as always,
any other library with its own cells and pins."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.pdk.command import cell_map_command
from faultflow.scan.techmap import (
    ScanTechmapConfig,
    render_scan_techmap,
    scan_techmap_config,
)

from .test_flop_liberty import LIBRARIES

ROOT = Path(__file__).resolve().parents[3]
HD_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.unit
def test_hds_techmap_is_the_one_faultflow_always_wrote(require_cpp_core: None) -> None:
    assert scan_techmap_config(HD_MAP) == ScanTechmapConfig()
    assert render_scan_techmap(scan_techmap_config(HD_MAP)) == render_scan_techmap()


@pytest.mark.unit
def test_a_library_without_scan_flops_keeps_the_default(require_cpp_core: None) -> None:
    """OSU035 has no scan flop: its flows never techmap (TechnologyProfile's
    supports_physical_scan), and the file written alongside stays what it was."""
    assert scan_techmap_config(ROOT / "cells/osu/osu035.json") == ScanTechmapConfig()


@pytest.mark.integration
@pytest.mark.parametrize("lib,liberty", LIBRARIES, ids=[lib for lib, _ in LIBRARIES])
def test_each_librarys_techmap_names_its_own_cells(
    lib: str, liberty: Path, tmp_path: Path, require_cpp_core: None
) -> None:
    out = tmp_path / f"{lib}.json"
    cell_map_command(liberty, out)
    cfg = scan_techmap_config(out)
    if lib == "sky130_fd_sc_hd":
        assert cfg == ScanTechmapConfig()
        return
    text = render_scan_techmap(cfg)
    assert "sky130_fd_sc_hd__" not in text
    for cell in (cfg.library_cell, cfg.and_cell, cfg.or_cell, cfg.inv_cell):
        assert cell.startswith(lib + "__") and cell in text
