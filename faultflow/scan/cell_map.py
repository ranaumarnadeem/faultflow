from __future__ import annotations

import json
from pathlib import Path

from faultflow.config import FaultflowConfig
from faultflow.scan.errors import ScanError

SCANFF_KEYS = (
    "$scanff_faultflow",
    "\\$scanff_faultflow",
    "$scanff_r_faultflow",
    "\\$scanff_r_faultflow",
    "$scanff_s_faultflow",
    "\\$scanff_s_faultflow",
)
INTERNAL_ATPG_VIEW_KEYS = (
    "$faultflow_observe_buf",
    "\\$faultflow_observe_buf",
    "$faultflow_d_branch_buf",
    "\\$faultflow_d_branch_buf",
    "$faultflow_capture_and",
    "\\$faultflow_capture_and",
    "$faultflow_capture_or",
    "\\$faultflow_capture_or",
    "$faultflow_capture_inv",
    "\\$faultflow_capture_inv",
)
OSU_CELL_MAP = Path("cells/osu/osu035.json")


def resolve_scan_cell_map(cfg: FaultflowConfig) -> Path:
    """Merged map: cfg.cell_lib plus abstract scan FF entries when missing."""
    base = json.loads(cfg.cell_lib.read_text(encoding="utf-8"))
    required = (*SCANFF_KEYS, *INTERNAL_ATPG_VIEW_KEYS)
    if all(key in base for key in required):
        return cfg.cell_lib
    osu = json.loads(OSU_CELL_MAP.read_text(encoding="utf-8"))
    merged = dict(base)
    for key in required:
        if key in osu and key not in merged:
            merged[key] = osu[key]
    missing = [key for key in required if key not in merged]
    if missing:
        # Never return a silently-incomplete cell map: a required scan/ATPG-view
        # cell absent from both the base map and the OSU fallback would otherwise
        # reach the C++ core as an unknown cell (hard fail) or be miscounted.
        raise ScanError(
            "scan cell map is missing required scan/ATPG-view cell definitions "
            f"(absent from both {cfg.cell_lib} and the {OSU_CELL_MAP} fallback): "
            + ", ".join(missing)
        )
    cache = cfg.intermediate_dir / "scan_merged_cell_map.json"
    cfg.ensure_workspace()
    text = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    # Avoid rewrite churn: resolve is called repeatedly per run, and the merged
    # content is deterministic from (base, OSU fallback).
    if not (cache.exists() and cache.read_text(encoding="utf-8") == text):
        cache.write_text(text, encoding="utf-8")
    return cache
