"""Which of a module's port bits get a boundary cell, and why the others don't.

Every data input and every output bit is wrapped, constant bits and feedthroughs
included. Left as they are:
- a clock: an input that reaches a flop's clock through buffers and inverters;
- an asynchronous clear or preset: an input that reaches a flop's clear or preset
  that way (a boundary cell there would let the reset follow the wrapper chain while
  it shifts);
- an input or output bit ``exclude`` names: a glob on the port, or on "port[bit]"
  -- ``*`` and ``?`` are wildcards, brackets match themselves (``b[1]``, ``b[*]``).

An input that reaches a clock, clear or preset only through other logic is refused,
and so is an inout port: name them in ``exclude``. So is a netlist already scanned or
wrapped: wrapping comes first.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.control_trace import Netlist, TraceError
from faultflow.scan.stitch import SCAN_CELL_TYPES
from faultflow.wrap.cell import CELL_ATTR
from faultflow.wrap.errors import WrapError

DATA = "data"
CLOCK = "clock"
CONTROL = "asynchronous clear or preset"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class PortBit:
    """One bit of a port: ``label`` is "port", or "port[bit]" on a bus."""

    port: str
    index: int
    label: str
    direction: str
    net: int | str


@dataclass(frozen=True)
class Decision:
    bit: PortBit
    wrapped: bool
    reason: str


def port_bits(module: Mapping[str, Any]) -> list[PortBit]:
    """Every bit of every input and output port, in netlist order."""
    bits: list[PortBit] = []
    for name, port in module.get("ports", {}).items():
        direction = str(port.get("direction", ""))
        width = len(port.get("bits", []))
        for index, net in enumerate(port.get("bits", [])):
            label = name if width == 1 else f"{name}[{index}]"
            bits.append(PortBit(name, index, label, direction, net))
    return bits


def _literal_brackets(glob: str) -> str:
    """``glob`` for fnmatch with its brackets matching themselves, not a set."""
    return "".join({"[": "[[]", "]": "[]]"}.get(char, char) for char in glob)


def _excluded(bit: PortBit, exclude: Sequence[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(name, _literal_brackets(glob))
        for glob in exclude
        for name in (bit.port, bit.label)
    )


def _refuse_scanned_or_wrapped(module: Mapping[str, Any]) -> None:
    for instance, cell in module.get("cells", {}).items():
        kind = str(cell.get("type", ""))
        if kind in SCAN_CELL_TYPES:
            raise WrapError(
                f"the netlist is already scanned ({instance} is a scan cell): wrap "
                "the design before scan insertion"
            )
        if kind.lstrip("\\").startswith("$wbc_") or CELL_ATTR in cell.get(
            "attributes", {}
        ):
            raise WrapError(f"the netlist is already wrapped ({instance})")


def _fan_in_ports(netlist: Netlist, net: Any) -> set[str]:
    """The input port bits in ``net``'s combinational fan-in (stopping at flops)."""
    found: set[str] = set()
    todo = [net]
    seen: set[int] = set()
    while todo:
        current = todo.pop()
        if not isinstance(current, int) or current in seen:
            continue
        seen.add(current)
        if current in netlist.input_net_names:
            found.add(netlist.input_net_names[current])
            continue
        if current not in netlist.drivers:
            continue
        driver, _pin = netlist.drivers[current]
        entry = netlist.entry(driver) or {}
        if entry.get("node_type") != "GATE":
            continue
        for pin, bits in netlist.cells[driver].get("connections", {}).items():
            if netlist.direction(driver, pin, entry) == "input":
                todo.extend(bits)
    return found


def classify(
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    exclude: Sequence[str] = (),
) -> list[Decision]:
    """Each input and output bit of ``module``: whether it gets a boundary cell, and
    why. Raises WrapError on what can't be wrapped."""
    _refuse_scanned_or_wrapped(module)
    netlist = Netlist(module, cell_map)
    try:
        flops = netlist.flops()
    except TraceError as exc:
        raise WrapError(str(exc)) from exc
    clocks: set[str] = set()
    controls: set[str] = set()
    through_logic: dict[str, str] = {}
    for flop in flops:
        pins = [(flop.clock_pin, flop.clock, clocks)]
        pins += [(c.pin, c.trace, controls) for c in flop.controls]
        for pin, trace, kind in pins:
            if trace.port is not None:
                kind.add(trace.port)
            elif trace.const is None and trace.steps:
                net = trace.steps[-1][0]
                for port in _fan_in_ports(netlist, net):
                    through_logic.setdefault(port, f"{flop.instance}.{pin}")
    decisions: list[Decision] = []
    for bit in port_bits(module):
        if bit.direction not in ("input", "output"):
            if _excluded(bit, exclude):
                decisions.append(Decision(bit, False, EXCLUDED))
                continue
            raise WrapError(
                f"{bit.label} is an {bit.direction} port, which FaultFlow can't "
                "wrap (it has no tristate): exclude it ([wrap] exclude)"
            )
        if _excluded(bit, exclude):
            decisions.append(Decision(bit, False, EXCLUDED))
        elif bit.direction == "input" and bit.label in clocks:
            decisions.append(Decision(bit, False, CLOCK))
        elif bit.direction == "input" and bit.label in controls:
            decisions.append(Decision(bit, False, CONTROL))
        elif bit.direction == "input" and bit.label in through_logic:
            raise WrapError(
                f"input {bit.label} reaches {through_logic[bit.label]} through "
                "logic: a boundary cell there would drive it while the wrapper "
                "shifts; exclude it ([wrap] exclude)"
            )
        else:
            decisions.append(Decision(bit, True, DATA))
    return decisions


def clock_ports(decisions: Sequence[Decision]) -> list[str]:
    """The input bits classified as clocks."""
    return [d.bit.label for d in decisions if d.reason == CLOCK]
