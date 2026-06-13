from __future__ import annotations

import json
from pathlib import Path

from faultflow.config import FaultflowConfig

SCANFF_KEYS = ("$scanff_faultflow", "\\$scanff_faultflow")
INTERNAL_ATPG_VIEW_KEYS = (
    "$faultflow_observe_buf",
    "\\$faultflow_observe_buf",
    "$faultflow_d_branch_buf",
    "\\$faultflow_d_branch_buf",
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
    cache = cfg.output_dir / "scan_merged_cell_map.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return cache
