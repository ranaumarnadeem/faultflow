"""Techmap cell-coverage audit (core logic).

Given a synthesized Yosys JSON netlist and a faultflow JSON cell map, report --
*before* any simulation runs:

  * every cell type used and its count,
  * memory / macro-like cell types (mem/ram/rom/sram/...), which silently leave
    the coverage denominator when blackboxed,
  * cell types NOT covered by the cell map (they would be blackboxed under the
    ``blackbox`` policy, or hard-fail under ``fail``).

This module owns the glob-match and counting logic. It is consumed both by the
``scripts/techmap_audit.py`` CLI wrapper and by the ``check_cells`` shell
command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MEMORY_KEYWORDS = ("mem", "ram", "rom", "sram", "dpram", "macro", "fifo", "regfile")


@dataclass(frozen=True)
class AuditResult:
    """Outcome of a techmap cell-coverage audit.

    ``mem_like``, ``uncovered`` and ``allowed_uncovered`` are lists of
    ``(cell_type, count)`` pairs sorted by descending count. ``ok`` is True when
    no uncovered (non-allow-listed) cell types remain.
    """

    netlist: Path
    cellmap: Path
    top: str
    total_cells: int
    type_counts: dict[str, int]
    mem_like: list[tuple[str, int]]
    uncovered: list[tuple[str, int]]
    allowed_uncovered: list[tuple[str, int]]

    @property
    def unique_types(self) -> int:
        return len(self.type_counts)

    @property
    def ok(self) -> bool:
        return not self.uncovered


def _strip_escape(name: str) -> str:
    """Yosys JSON sometimes escapes internal names with a leading backslash."""
    return name[1:] if name.startswith("\\") else name


def cellmap_covers(cell_type: str, patterns: list[str]) -> bool:
    """Mirror the cell-map glob match: exact key, or a 'prefix*' pattern.

    Leading backslash escapes are normalised on both sides so '\\$scanff_...'
    keys match '$scanff_...' types and vice versa.
    """
    ct = _strip_escape(cell_type)
    for pat in patterns:
        p = _strip_escape(pat)
        if p.endswith("*"):
            if ct.startswith(p[:-1]):
                return True
        elif ct == p:
            return True
    return False


def _top_module(net: dict, requested: str | None) -> tuple[str, dict]:
    modules = net.get("modules", {})
    if requested and requested in modules:
        return requested, modules[requested]
    for name, mod in modules.items():
        attrs = mod.get("attributes", {})
        top = str(attrs.get("top", "0"))
        if top in ("1", "00000000000000000000000000000001"):
            return name, mod
    # Fall back to the only / first module.
    name = next(iter(modules))
    return name, modules[name]


def audit_netlist(
    netlist: Path,
    cellmap: Path,
    allow: list[str],
    *,
    top: str | None = None,
) -> AuditResult:
    """Audit ``netlist`` against the JSON ``cellmap`` (techmap).

    ``allow`` is a list of cell-type globs to treat as intentional blackboxes.
    ``top`` selects the module to audit; when omitted the top is auto-detected.
    """
    net = json.loads(netlist.read_text(encoding="utf-8"))
    cmap = json.loads(cellmap.read_text(encoding="utf-8"))
    patterns = list(cmap.keys())

    top_name, mod = _top_module(net, top)
    cells = mod.get("cells", {})

    type_counts: dict[str, int] = {}
    for cell in cells.values():
        t = str(cell.get("type", "?"))
        type_counts[t] = type_counts.get(t, 0) + 1

    mem_like = sorted(
        (
            (t, n)
            for t, n in type_counts.items()
            if any(k in t.lower() for k in MEMORY_KEYWORDS)
        ),
        key=lambda x: -x[1],
    )
    uncovered = sorted(
        (
            (t, n)
            for t, n in type_counts.items()
            if not cellmap_covers(t, patterns) and not cellmap_covers(t, allow)
        ),
        key=lambda x: -x[1],
    )
    allowed_uncovered = sorted(
        (
            (t, n)
            for t, n in type_counts.items()
            if not cellmap_covers(t, patterns) and cellmap_covers(t, allow)
        ),
        key=lambda x: -x[1],
    )

    return AuditResult(
        netlist=netlist,
        cellmap=cellmap,
        top=top_name,
        total_cells=len(cells),
        type_counts=type_counts,
        mem_like=mem_like,
        uncovered=uncovered,
        allowed_uncovered=allowed_uncovered,
    )
