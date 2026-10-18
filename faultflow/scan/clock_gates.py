"""Integrated clock gates (a cell map's ``node_type: ICG`` cells) in a scan netlist.

A gated flop's clock pin is a clock gate's output; its *root* clock is the clock the
gate -- or the chain of gates, one clocking the next -- reads at the far end: the
clock that pulses it, which scan stitching groups it by and the manifest records. Its
pin stays on the gated clock.

In a scan test every gate must be open while the chains shift: its test enable is the
scan enable (stitching connects one the netlist ties off, and puts the library's gate
with a test enable in place of one without), or its enable is held so. In capture a
gate closed holds its flops, as a clock enable would: the ATPG view captures
``enable ? D : Q`` for each, ``enable`` the gate's -- every gate's on the path, ANDed.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping

from faultflow.scan.errors import ScanError


@dataclass(frozen=True)
class ClockGate:
    instance: str
    cell_type: str
    clock_pin: str
    enable_pin: str
    test_pin: str  # "" for a gate without one
    output_pin: str
    edge: str  # POSEDGE | NEGEDGE
    clock_net: Any  # an int net, or a constant / "x" string
    output_net: int


def _entry(cell_map: Mapping[str, Any], cell_type: str) -> Mapping[str, Any] | None:
    exact = cell_map.get(cell_type)
    if isinstance(exact, Mapping):
        return exact
    for pattern, entry in cell_map.items():
        if isinstance(entry, Mapping) and fnmatch.fnmatchcase(cell_type, pattern):
            return entry
    return None


def clock_gates(
    module: Mapping[str, Any], cell_map: Mapping[str, Any]
) -> dict[int, ClockGate]:
    """Every clock gate of ``module``, by the net it drives."""
    found: dict[int, ClockGate] = {}
    entries: dict[str, Mapping[str, Any] | None] = {}
    for instance, cell in (module.get("cells") or {}).items():
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("type", ""))
        if cell_type not in entries:
            entries[cell_type] = _entry(cell_map, cell_type)
        entry = entries[cell_type]
        if entry is None or entry.get("node_type") != "ICG":
            continue
        icg = entry.get("icg") or {}
        conns = cell.get("connections") or {}
        out = conns.get(str(icg["output"])) or []
        if len(out) != 1 or not isinstance(out[0], int):
            continue  # clocks nothing
        clock = conns.get(str(icg["clock"])) or ["x"]
        found[out[0]] = ClockGate(
            instance=str(instance),
            cell_type=cell_type,
            clock_pin=str(icg["clock"]),
            enable_pin=str(icg["enable"]),
            test_pin=str(icg.get("test", "")),
            output_pin=str(icg["output"]),
            edge=str(icg.get("edge", "POSEDGE")),
            clock_net=clock[0],
            output_net=out[0],
        )
    return found


def root_clock(
    net: Any, gates: Mapping[int, ClockGate]
) -> tuple[Any, tuple[ClockGate, ...]]:
    """The clock ``net`` is gated from, and the gates on the way, the nearest
    first."""
    path: list[ClockGate] = []
    while isinstance(net, int) and net in gates:
        gate = gates[net]
        if any(g.instance == gate.instance for g in path):
            raise ScanError(f"clock gate {gate.instance} gates its own clock")
        path.append(gate)
        net = gate.clock_net
    return net, tuple(path)
