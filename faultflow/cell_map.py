from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def pattern_match(pattern: str, name: str) -> bool:
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return pattern == name


def load_cell_map(path: Path) -> list[tuple[str, dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"cell map root must be an object: {path}")
    return list(data.items())


def lookup_cell(
    cell_type: str, entries: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any] | None:
    for pattern, entry in entries:
        if pattern_match(pattern, cell_type):
            return entry
    return None


def is_supported_or_deferred(
    cell_type: str, entries: list[tuple[str, dict[str, Any]]]
) -> bool:
    entry = lookup_cell(cell_type, entries)
    if entry is None:
        return False
    return bool(entry.get("unsupported")) or "node_type" in entry
