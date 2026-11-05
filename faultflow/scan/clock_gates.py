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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from faultflow.scan.errors import ScanError

if TYPE_CHECKING:
    from faultflow.control_trace import Netlist


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


def clock_gates(
    module: Mapping[str, Any], cell_map: Mapping[str, Any]
) -> dict[int, ClockGate]:
    """Every clock gate of ``module``, by the net it drives."""
    from faultflow.scan.stitch import _lookup_cell  # it imports this module

    found: dict[int, ClockGate] = {}
    entries: dict[str, Mapping[str, Any] | None] = {}
    for instance, cell in (module.get("cells") or {}).items():
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("type", ""))
        if cell_type not in entries:
            hit = _lookup_cell(cell_map, cell_type)
            entries[cell_type] = hit[1] if hit is not None else None
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


@dataclass(frozen=True)
class ClockPath:
    """Where a flop's clock comes from: ``root``, the net the path stops at (an
    input port's when a port pulses it), the clock gates on the way, the nearest
    first, whether the cells on it invert the clock, every net on it (the pin's
    first, the root's last), and the held side inputs that keep it passing the
    clock on (control_trace.Step: net, the cell reading it, its pin, the value)."""

    root: Any
    gates: tuple[ClockGate, ...]
    inverted: bool
    nets: tuple[int, ...]
    holds: tuple[tuple[int, str, str, int], ...]


def clock_path(
    net: Any,
    gates: Mapping[int, ClockGate],
    netlist: "Netlist | None" = None,
    holds: Mapping[str, int] | None = None,
) -> ClockPath:
    """Follow the clock ``net`` back through clock gates, and with ``netlist``
    through every cell that passes it on while ``holds`` (input port bits at their
    values, ``[scan] hold``) fix its other inputs -- a buffer, an inverter, a mux a
    held select steers, an AND whose other input is held at 1
    (control_trace.Netlist.through) -- to the first net none of them drives."""
    path: list[ClockGate] = []
    nets: list[int] = []
    steps: list[tuple[int, str, str, int]] = []
    inverted = False
    values = dict(holds or {})
    seen: set[int] = set()
    while isinstance(net, int) and net not in seen:  # not round a loop
        seen.add(net)
        nets.append(net)
        if net in gates:
            gate = gates[net]
            if any(g.instance == gate.instance for g in path):
                raise ScanError(f"clock gate {gate.instance} gates its own clock")
            path.append(gate)
            net = gate.clock_net
            continue
        if netlist is None:
            break
        found = netlist.through(net, values)
        if found.source == net:
            break
        passed = [n for n in found.nets[1:] if n != found.source and n not in seen]
        nets.extend(passed)
        seen.update(passed)
        inverted ^= found.inverted
        steps.extend(found.steps)
        net = found.source
    return ClockPath(net, tuple(path), inverted, tuple(nets), tuple(steps))


def clock_path_problems(
    module: Mapping[str, Any],
    manifest: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    holds: Mapping[str, int],
) -> list[str]:
    """What keeps the stitched ``module``'s scan flops off the root clocks the
    manifest records, with ``holds`` (``[scan] hold`` as it is now): each flop's
    clock traced again from its pin (:func:`clock_path`). Empty when every one
    still reaches its port, not inverted -- a hold changed since stitching, say,
    would let a clock mux pick another clock."""
    from faultflow.control_trace import Netlist  # it imports scan.stitch

    netlist = Netlist(module, cell_map)
    gates = clock_gates(module, cell_map)
    nets = [int(net) for net in manifest.get("clock_nets") or []]
    names = dict(zip(nets, [str(p) for p in manifest.get("clock_ports") or []]))
    cells = module.get("cells") or {}
    moved: list[str] = []
    paths: dict[Any, ClockPath] = {}  # by pin net: flops on one leaf share it
    for row in manifest.get("cells") or []:
        if not isinstance(row, dict) or row.get("instance") not in cells:
            continue
        instance = str(row["instance"])
        pin = (cells[instance].get("connections") or {}).get("CLK") or []
        if len(pin) != 1:
            continue
        if pin[0] not in paths:
            paths[pin[0]] = clock_path(pin[0], gates, netlist, holds)
        found = paths[pin[0]]
        if found.root != row.get("clock_net") or found.inverted:
            moved.append(instance)
    if not moved:
        return []
    held = sorted(
        {
            f"{hold['port']}:{hold['value']}"
            for path in (manifest.get("clock_paths") or {}).values()
            for hold in path.get("holds", [])
        }
    )
    roots = sorted(set(names.values())) or [str(net) for net in nets]
    shown = ", ".join(moved[:3]) + (f" and {len(moved) - 3} more" if moved[3:] else "")
    return [
        f"the clocks of scan flops {shown} no longer reach "
        f"{', '.join(roots)} as scan insertion traced them"
        + (f", through cells it found held by {', '.join(held)}" if held else "")
        + "; [scan] hold changed since: run ff.py scan again"
    ]
