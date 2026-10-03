"""A scan flop's clear and preset while the chains shift.

A real scan flop's clear and preset act whatever its scan enable is -- sky130's sdfrtp
is a scan mux in front of a flop ``RESET_B`` clears -- so one that toggles, or sits
active, while the chains shift clears or presets the flop mid-load or mid-unload.
FaultFlow's simulators don't model that: they turn a scan flop's clear and preset off
while scan enable is active. A pattern's load and unload are what FaultFlow computes
only if every scan flop's clear and preset is *held inactive during shift*: traced
(:mod:`faultflow.control_trace`) to constants, to inputs held for the whole test --
scan enable at 1, ``[scan] hold`` and the reset holds -- and to the non-scan flops the
scan test ties (forced or settled: :mod:`faultflow.scan.nonscan`). A reset from a
scanned flop -- a reset synchronizer's last stage -- or from an input the patterns set
isn't; ``scan-check`` refuses those flops (:func:`shift_control_violations`).

The *reset holds* (:func:`reset_holds`): an input that reaches a scan flop's clear or
preset through buffers and inverters is held for the whole test at the value that
keeps the pin inactive -- the usual ``rst_n``, or an active-high ``rst`` through an
inverter. An input two such pins need at opposite values isn't held.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.control_trace import CONTROLLING, MUXES, Netlist, one_net
from faultflow.scan.nonscan import NonscanSetup
from faultflow.scan.stitch import SCAN_CELL_TYPES


@dataclass(frozen=True)
class ShiftViolation:
    instance: str  # the scan flop
    pin: str  # its clear or preset pin
    reason: str

    def __str__(self) -> str:
        return f"{self.pin} of {self.instance}: {self.reason}"


def _scan_cells(netlist: Netlist) -> list[str]:
    return sorted(
        instance
        for instance, cell in netlist.cells.items()
        if cell.get("type") in SCAN_CELL_TYPES
    )


def _reset_holds(netlist: Netlist, ports: Mapping[str, Any]) -> dict[str, int]:
    wanted: dict[str, set[int]] = {}
    for instance in _scan_cells(netlist):
        for control in netlist.controls(instance):
            port = control.trace.port
            port_def = ports.get(port) if port is not None else None
            if port is None or not isinstance(port_def, dict):
                continue
            if len(port_def.get("bits", [])) != 1:
                continue
            inactive = 1 - control.active
            wanted.setdefault(port, set()).add(inactive ^ int(control.trace.inverted))
    return {port: values.pop() for port, values in wanted.items() if len(values) == 1}


def reset_holds(
    module: Mapping[str, Any], cell_map: Mapping[str, Any]
) -> dict[str, int]:
    """The inputs that reach a scan flop's clear or preset through buffers and
    inverters, each with the value that keeps those pins inactive (see the module
    docstring)."""
    return _reset_holds(Netlist(module, cell_map), module.get("ports", {}))


def _unheld(
    netlist: Netlist,
    net: Any,
    values: Mapping[str, int],
    known: Mapping[int, int],
    scan: set[str],
) -> list[str]:
    """What keeps ``net`` from being held, at most three: the inputs the patterns
    set, the flops and the logic its cone starts at."""
    found: list[str] = []
    seen: set[int] = set()
    stack = [net]
    while stack and len(found) < 3:
        current = stack.pop()
        if isinstance(current, str):
            if current not in ("0", "1"):
                found.append("an unconnected net")
            continue
        if not isinstance(current, int) or current in seen:
            continue
        seen.add(current)
        if current in netlist.input_net_names:
            name = netlist.input_net_names[current]
            if name not in values:
                found.append(f"the input {name}, which the patterns set")
            continue
        if current in known:
            continue
        if current not in netlist.drivers:
            found.append(f"net {current}, which nothing drives")
            continue
        instance = netlist.drivers[current][0]
        entry = netlist.entry(instance) or {}
        gate = str(entry.get("gate_type", ""))
        traced = gate in ("BUF", "INV") or gate in CONTROLLING or gate in MUXES
        if entry.get("node_type") == "FF":
            kind = "scan flop" if instance in scan else "non-scan flop"
            found.append(f"the output of {kind} {instance}")
        elif entry.get("node_type") == "CONST" or gate in ("CONST0", "CONST1"):
            continue
        elif entry.get("node_type") != "GATE" or not traced:
            cell_type = gate or str(netlist.cells[instance].get("type", ""))
            found.append(
                f"{instance} (a {cell_type}, which the check can't see through)"
            )
        else:
            conns = netlist.cells[instance].get("connections", {})
            for pin in reversed(entry.get("inputs", [])):
                stack.append(one_net(conns.get(str(pin))))
    return found


def shift_control_violations(
    module: Mapping[str, Any],
    manifest: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    *,
    holds: Mapping[str, int],
    nonscan: NonscanSetup | None,
) -> list[ShiftViolation]:
    """The scan flops of the stitched ``module`` whose clear or preset isn't held
    inactive during shift, one per pin (see the module docstring)."""
    netlist = Netlist(module, cell_map)
    values = {**_reset_holds(netlist, module.get("ports", {})), **holds}
    scan_enable = str(manifest.get("scan_enable", ""))
    if scan_enable:
        values[scan_enable] = 1
    known = {
        flop.q: int(flop.value)
        for flop in (nonscan.flops if nonscan is not None else ())
        if flop.value is not None
    }
    scan = set(_scan_cells(netlist))
    found: list[ShiftViolation] = []
    for instance in sorted(scan):
        for control in netlist.controls(instance):
            held = netlist.forced(instance, control.pin, values, known)
            if held is None:
                net = one_net(netlist.cells[instance]["connections"].get(control.pin))
                sources = _unheld(netlist, net, values, known, scan) or [
                    "logic the check can't show held"
                ]
                reason = "not held: it depends on " + ", ".join(sources)
            elif held.value == control.active:
                verb = "clears" if control.kind == "clear" else "presets"
                reason = f"held at its active level, which {verb} it the whole shift"
            else:
                continue
            found.append(ShiftViolation(instance, control.pin, reason))
    return found


def shift_control_message(violations: Sequence[ShiftViolation]) -> str:
    """scan-check's error: the flops grouped by pin and reason, three named each."""
    groups: dict[tuple[str, str], list[str]] = {}
    for violation in violations:
        groups.setdefault((violation.pin, violation.reason), []).append(
            violation.instance
        )
    parts = []
    for (pin, reason), names in groups.items():
        shown = ", ".join(names[:3])
        if len(names) > 3:
            shown += f" and {len(names) - 3} more"
        parts.append(f"{pin} of {shown}: {reason}")
    return (
        "scan flops whose clear or preset isn't held inactive during shift -- "
        + "; ".join(parts)
        + ". A real scan flop's clear and preset act while the chains shift, but "
        "FaultFlow's simulation turns them off, so these patterns wouldn't unload "
        "what FaultFlow expects. Hold the input that keeps it inactive ([scan] "
        "hold), list the flops that drive it in [scan] nonscan_cells if a scan "
        "clock settles them (a reset synchronizer), or gate it in the RTL with scan "
        "enable or a held test-mode input; [scan] shift_controls = warn accepts them"
    )
