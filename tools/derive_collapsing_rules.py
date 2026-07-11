#!/usr/bin/env python3
"""Derive stuck-at fault equivalence classes for compound cells from truth tables.

For each gate's boolean function, this enumerates every fault site (each input
SA0/SA1 and the output SA0/SA1), computes the exact set of input vectors that
detect it, and groups faults whose detecting-vector sets are identical. Those
groups are the equivalence classes the collapser may safely collapse (provided
every INPUT member is fanout-free in the netlist).

This is the authoritative source for the C++ `compound_classes_for` table in
src/core/fault/collapser/fault_collapser.cpp and for docs/collapsing_rules.md.
Run: python3 tools/derive_collapsing_rules.py

The inN order matches the C++ eval_bitwise switch and the sky130 cell-map inputs.
"""

from __future__ import annotations

from itertools import product
from typing import Callable

# Gate boolean functions keyed by GateType name (inN order = cell-map input order).
GATES: dict[str, tuple[int, Callable[[list[bool]], bool]]] = {
    "A21OI": (3, lambda i: not ((i[0] and i[1]) or i[2])),  # AOI21
    "O21AI": (3, lambda i: not ((i[0] or i[1]) and i[2])),  # OAI21
    "A22OI": (4, lambda i: not ((i[0] and i[1]) or (i[2] and i[3]))),  # AOI22
    "O22AI": (4, lambda i: not ((i[0] or i[1]) and (i[2] or i[3]))),  # OAI22
    "A21O": (3, lambda i: (i[0] and i[1]) or i[2]),
    "O21A": (3, lambda i: (i[0] or i[1]) and i[2]),
    "A22O": (4, lambda i: (i[0] and i[1]) or (i[2] and i[3])),
    "O22A": (4, lambda i: (i[0] or i[1]) and (i[2] or i[3])),
    # XOR/XNOR are listed to prove they yield NO equivalence classes.
    "XOR2": (2, lambda i: i[0] ^ i[1]),
    "XNOR2": (2, lambda i: not (i[0] ^ i[1])),
}


Site = tuple[str, int, int]  # (kind, input_index or -1 for output, stuck value)


def detecting_sets(n: int, f: Callable[[list[bool]], bool]) -> dict[Site, frozenset]:
    """Map each fault site to its frozenset of detecting input vectors."""
    out: dict[Site, frozenset] = {}
    for k in range(n):
        for v in (0, 1):
            detected = set()
            for vec in product([False, True], repeat=n):
                fv = list(vec)
                fv[k] = bool(v)
                if f(fv) != f(list(vec)):
                    detected.add(vec)
            out[("in", k, v)] = frozenset(detected)
    for v in (0, 1):
        detected = set()
        for vec in product([False, True], repeat=n):
            if bool(v) != f(list(vec)):
                detected.add(vec)
        out[("out", -1, v)] = frozenset(detected)
    return out


def site_name(site: Site) -> str:
    if site[0] == "in":
        return f"in{site[1]}_SA{site[2]}"
    return f"out_SA{site[2]}"


def equivalence_classes(n: int, f: Callable[[list[bool]], bool]) -> list[list[Site]]:
    by_set: dict[frozenset, list[Site]] = {}
    for site, detected in detecting_sets(n, f).items():
        if not detected:
            continue  # untestable site; not a collapse candidate
        by_set.setdefault(detected, []).append(site)
    return [sites for sites in by_set.values() if len(sites) > 1]


def main() -> None:
    for gate, (n, f) in GATES.items():
        classes = equivalence_classes(n, f)
        if classes:
            rendered = "; ".join(
                "{" + ", ".join(site_name(s) for s in cls) + "}" for cls in classes
            )
        else:
            rendered = "NO equivalence classes (not collapsible)"
        print(f"{gate}: {rendered}")


if __name__ == "__main__":
    main()
