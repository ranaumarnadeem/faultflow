#!/usr/bin/env python3
"""Techmap coverage audit.

Point this at a synthesized Yosys JSON netlist and a faultflow JSON cell map to
find, *before* running any simulation:

  * every cell type used and its count,
  * memory / macro-like cell types (mem/ram/rom/sram/...), which silently leave
    the coverage denominator when blackboxed,
  * cell types NOT covered by the cell map (they would be blackboxed under the
    `blackbox` policy, or hard-fail under `fail`).

This is the side-by-side companion for extending a techmap to a new core: run it
on the new netlist, and it tells you exactly which cell types you still need to
add to the JSON cell map.

Exit code is 0 when every cell type is covered or explicitly allow-listed, and 1
when uncovered cell types remain -- so it can gate a CI / preflight step.

Usage:
    python3 scripts/techmap_audit.py NETLIST.json CELLMAP.json [--top TOP]
                                     [--allow PATTERN ...]

Example:
    python3 scripts/techmap_audit.py \\
        examples/picorv32_synth/picorv32a_sky130.json \\
        cells/sky130/sky130_fd_sc_hd.json --allow '$scopeinfo'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MEMORY_KEYWORDS = ("mem", "ram", "rom", "sram", "dpram", "macro", "fifo", "regfile")


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


def audit(netlist: Path, cellmap: Path, top: str | None, allow: list[str]) -> int:
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
        ((t, n) for t, n in type_counts.items()
         if any(k in t.lower() for k in MEMORY_KEYWORDS)),
        key=lambda x: -x[1],
    )
    uncovered = sorted(
        ((t, n) for t, n in type_counts.items()
         if not cellmap_covers(t, patterns) and not cellmap_covers(t, allow)),
        key=lambda x: -x[1],
    )
    allowed_uncovered = sorted(
        ((t, n) for t, n in type_counts.items()
         if not cellmap_covers(t, patterns) and cellmap_covers(t, allow)),
        key=lambda x: -x[1],
    )

    print(f"netlist : {netlist}")
    print(f"cellmap : {cellmap}")
    print(f"top     : {top_name}")
    print(f"cells   : {len(cells)}  unique types: {len(type_counts)}")

    print("\nMEMORY / MACRO-LIKE cell types (excluded from denominator if blackboxed):")
    if mem_like:
        for t, n in mem_like:
            print(f"    {t}: {n}")
    else:
        print("    NONE")

    if allowed_uncovered:
        print("\nUNCOVERED but ALLOW-LISTED (intentionally blackboxed):")
        for t, n in allowed_uncovered:
            print(f"    {t}: {n}")

    print("\nUNCOVERED cell types (add these to the cell map):")
    if uncovered:
        for t, n in uncovered:
            print(f"    {t}: {n}")
    else:
        print("    NONE -- every cell type is covered or allow-listed.")

    if uncovered:
        print(f"\nFAIL: {len(uncovered)} cell type(s) not in the techmap.")
        return 1
    print("\nOK: techmap covers every cell type in this netlist.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit a netlist against a faultflow JSON cell map (techmap).",
    )
    ap.add_argument("netlist", type=Path, help="Synthesized Yosys JSON netlist")
    ap.add_argument("cellmap", type=Path, help="faultflow JSON cell map")
    ap.add_argument("--top", help="Top module name (auto-detected if omitted)")
    ap.add_argument(
        "--allow",
        nargs="*",
        default=[],
        metavar="PATTERN",
        help="Cell-type globs to treat as intentional blackboxes (e.g. '$scopeinfo')",
    )
    args = ap.parse_args()
    return audit(args.netlist, args.cellmap, args.top, list(args.allow))


if __name__ == "__main__":
    sys.exit(main())
