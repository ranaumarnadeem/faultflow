#!/usr/bin/env python3
"""Techmap coverage audit (thin CLI wrapper).

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

The audit logic lives in :mod:`faultflow.reporter.cell_audit`; this file is only
the CLI + printing front end. The `check_cells` shell command shares the same
core.

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
import sys
from pathlib import Path

# Allow running the script directly from a checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faultflow.reporter.cell_audit import audit_netlist  # noqa: E402


def audit(netlist: Path, cellmap: Path, top: str | None, allow: list[str]) -> int:
    result = audit_netlist(netlist, cellmap, allow, top=top)

    print(f"netlist : {result.netlist}")
    print(f"cellmap : {result.cellmap}")
    print(f"top     : {result.top}")
    print(f"cells   : {result.total_cells}  unique types: {result.unique_types}")

    print("\nMEMORY / MACRO-LIKE cell types (excluded from denominator if blackboxed):")
    if result.mem_like:
        for t, n in result.mem_like:
            print(f"    {t}: {n}")
    else:
        print("    NONE")

    if result.allowed_uncovered:
        print("\nUNCOVERED but ALLOW-LISTED (intentionally blackboxed):")
        for t, n in result.allowed_uncovered:
            print(f"    {t}: {n}")

    print("\nUNCOVERED cell types (add these to the cell map):")
    if result.uncovered:
        for t, n in result.uncovered:
            print(f"    {t}: {n}")
    else:
        print("    NONE -- every cell type is covered or allow-listed.")

    if result.uncovered:
        print(f"\nFAIL: {len(result.uncovered)} cell type(s) not in the techmap.")
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
