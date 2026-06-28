from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.scan import cell_map as cell_map_mod
from faultflow.scan.cell_map import (
    INTERNAL_ATPG_VIEW_KEYS,
    SCANFF_KEYS,
    resolve_scan_cell_map,
)
from faultflow.scan.errors import ScanError

ROOT = Path(__file__).resolve().parents[3]
SKY130 = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
OSU035 = ROOT / "cells/osu/osu035.json"
REQUIRED = (*SCANFF_KEYS, *INTERNAL_ATPG_VIEW_KEYS)


def _cfg_with_cell_lib(tmp_path: Path, cell_lib: Path):
    source = tmp_path / "dummy.json"
    source.write_text(
        json.dumps({"modules": {"top": {"attributes": {"top": "1"}}}}) + "\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {source}
cell_lib = {cell_lib}
[simulation]
unsupported_cells = fail
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, "top")
    cfg.ensure_workspace()
    return cfg


def _assert_all_required(resolved: Path) -> None:
    data = json.loads(resolved.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED if key not in data]
    assert missing == [], f"resolved scan cell map missing {missing}"


def test_resolve_scan_cell_map_sky130_has_all_required(tmp_path: Path) -> None:
    cfg = _cfg_with_cell_lib(tmp_path, SKY130)
    _assert_all_required(resolve_scan_cell_map(cfg))


def test_resolve_scan_cell_map_osu035_has_all_required(tmp_path: Path) -> None:
    # osu035 must self-supply every scan/ATPG-view cell (no silent merge gap).
    cfg = _cfg_with_cell_lib(tmp_path, OSU035)
    _assert_all_required(resolve_scan_cell_map(cfg))


def test_resolve_scan_cell_map_merges_missing_key_from_fallback(tmp_path: Path) -> None:
    # A base map missing an internal cell that the OSU fallback supplies must be
    # healed by the merge (not raise), and the result must contain every key.
    base = json.loads(SKY130.read_text(encoding="utf-8"))
    base.pop("$faultflow_capture_and", None)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    cfg = _cfg_with_cell_lib(tmp_path, base_path)
    _assert_all_required(resolve_scan_cell_map(cfg))


def test_resolve_scan_cell_map_raises_when_key_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A required key absent from BOTH the base map and the fallback must raise a
    # clear ScanError, never silently produce an incomplete cell map.
    base = json.loads(SKY130.read_text(encoding="utf-8"))
    base.pop("$scanff_r_faultflow", None)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    empty_fallback = tmp_path / "empty_osu.json"
    empty_fallback.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cell_map_mod, "OSU_CELL_MAP", empty_fallback)
    cfg = _cfg_with_cell_lib(tmp_path, base_path)
    with pytest.raises(ScanError, match="scanff_r_faultflow"):
        resolve_scan_cell_map(cfg)
