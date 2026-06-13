from __future__ import annotations

from pathlib import Path


def build_site_key_index(
    core: object,
    json_path: str | Path,
    cell_map_path: str | Path,
    unsupported: str,
) -> dict[str, int]:
    rows = core.list_site_keys(str(json_path), str(cell_map_path), unsupported)
    return {str(row["site_key"]): int(row["compiled_net_index"]) for row in rows}


def fault_type_to_sa_code(fault_type: str) -> int:
    return 0 if fault_type == "sa0" else 1
