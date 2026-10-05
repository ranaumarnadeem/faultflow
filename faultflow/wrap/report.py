"""A wrapped block's coverage in a wrapper test mode, by part (the coverage report's
"wrapper" entry): its core, its wrapper's boundary cells, and its mode pins.

A fault site belongs to the boundary when it is on a boundary cell: a branch into
one of its cells, or a stem one of them drives -- or an input cell's port bit,
which nothing in the block drives. A site on a mode net belongs to the mode pins.
Every other site is the core's. Each part counts as the summary counts: what the
test detects of what it grades, what it leaves to the other mode
(``wbr_decoupled``), and what only the environment (``blackbox_unresolved``) or
free holds (``hold_unresolved``) could test.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from faultflow.control_trace import Netlist
from faultflow.wrap.cell import INPUT, ROLES

PARTS = ("core", "boundary", "mode")


def _site(key: str) -> tuple[int, str | None]:
    """A fault site key's net, and the cell a branch site reads it into."""
    prefix, net, rest = key.split(":", 2)
    if prefix != "net":
        raise ValueError(f"not a fault site key: {key}")
    if rest == "stem":
        return int(net), None
    kind, consumer = rest.split(":", 1)
    if kind != "branch":
        raise ValueError(f"not a fault site key: {key}")
    return int(net), consumer.rsplit(":", 1)[0]


def wrapper_coverage(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    """The "wrapper" entry of the coverage report of `campaign_id`, a test in
    `mode` of the block `module` (its scanned netlist) and its `wrapper` (the scan
    manifest's)."""
    cells = {str(cell[role]) for cell in wrapper["cells"] for role in ROLES}
    mode_nets = {int(wrapper["intest"]["net"]), int(wrapper["extest"]["net"])}
    port_bits = {
        int(cell["sys_net"])
        for cell in wrapper["cells"]
        if cell["side"] == INPUT and isinstance(cell["sys_net"], int)
    }
    drivers = Netlist(module, cell_map).drivers

    def part(key: str) -> str:
        net, consumer = _site(key)
        if net in mode_nets:
            return "mode"
        if consumer is not None:
            return "boundary" if consumer in cells else "core"
        driver = drivers.get(net)
        on_cell = driver is not None and driver[0] in cells
        return "boundary" if on_cell or net in port_bits else "core"

    counts = {
        name: {
            "detected": 0,
            "denominator": 0,
            "decoupled": 0,
            "blackbox_unresolved": 0,
            "hold_unresolved": 0,
        }
        for name in PARTS
    }
    for row in conn.execute(
        """
        SELECT fault_site_key,
          (exclusion = 'none' AND collapsed_into IS NULL
           AND status != 'redundant') AS counted,
          (status = 'detected' AND exclusion = 'none'
           AND collapsed_into IS NULL) AS detected,
          (exclusion = 'wbr_decoupled') AS decoupled,
          (blackbox_unresolved = 1 AND exclusion = 'none'
           AND collapsed_into IS NULL AND status != 'detected') AS unresolved,
          (hold_unresolved = 1 AND exclusion = 'none'
           AND collapsed_into IS NULL AND status != 'detected') AS held
        FROM faults
        WHERE campaign_id = ?
        """,
        (campaign_id,),
    ):
        entry = counts[part(str(row[0]))]
        entry["denominator"] += int(row[1])
        entry["detected"] += int(row[2])
        entry["decoupled"] += int(row[3])
        entry["blackbox_unresolved"] += int(row[4])
        entry["hold_unresolved"] += int(row[5])
    parts: dict[str, dict[str, Any]] = {}
    for name, entry in counts.items():
        denominator = entry["denominator"]
        parts[name] = {
            **entry,
            "coverage_percent": (
                100.0 * entry["detected"] / denominator if denominator else None
            ),
        }
    return {"mode": mode, "cells": len(wrapper["cells"]), "parts": parts}


def wrapper_lines(entry: Mapping[str, Any]) -> list[str]:
    """The coverage report's text for its "wrapper" entry."""
    lines = [
        f"wrapper ({entry['mode']}, {entry['cells']} boundary cells): detected / "
        "denominator, left to the other mode, blackbox_unresolved, hold_unresolved",
    ]
    for name in PARTS:
        part = entry["parts"][name]
        percent = part["coverage_percent"]
        shown = f"{percent:7.3f}%" if percent is not None else "      -"
        lines.append(
            f"  {name:9s} {part['detected']:6d} / {part['denominator']:<6d} {shown}"
            f"  {part['decoupled']:6d}  {part['blackbox_unresolved']:6d}"
            f"  {part['hold_unresolved']:6d}"
        )
    return lines
