"""Non-scan cells in a scan test: ``[scan] nonscan_cells`` and ``[scan] hold``.

A flop whose instance matches a ``nonscan_cells`` glob stays out of the scan chains (the
stitcher records it as ineligible, reason ``nonscan_policy``) -- a JTAG TAP and its
IJTAG network, run non-scan and tested through TCK by ``ff.py jtag``. The scan view is
one combinational frame between load and unload, so such a flop must hold still for the
whole test. Each must be one of:

- *forced*: a held input or a constant keeps its clear or preset active, traced through
  buffers and inverters only. Its output is the value that control forces, and the view
  ties it there (``$faultflow_tie0``/``$faultflow_tie1``).
- *frozen*: its clock is held or constant and no free input can clear or preset it.
  Its value is unknown, so its output is an X source, masked like an unknown blackbox
  output.

Anything else refuses. ``[scan] hold`` inputs are held in every cycle of every scan
pattern; the view ties them (their port removed), so ATPG can't assign them either. A
hold must be a single-bit input, can't be a scan port or a scan clock, and can't put a
scan flop's clear or preset in its active level.

A stuck-at on a forcing control's traced path can release a forced flop in the faulty
machine, which a tie can't show: :attr:`NonscanSetup.release_faults` lists those (site
key, fault type) pairs, which scan leaves to JTAG.

A fault the ties alone make untestable -- glue logic a TDR held at its reset value
gates, say -- is hold_unresolved, not redundant: SAT finds a test on the view's hold
twin (atpg_view.make_nonscan_free), where they are free.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.jtag.xcheck import (
    JtagSetupError,
    _held_value,
    _Netlist,
    _port_bits,
    _reset_path_faults,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.manifest import manifest_clock_net_ids

NONSCAN_REASON = "nonscan_policy"


@dataclass(frozen=True)
class NonscanFlop:
    instance: str
    q: int  # its output net
    value: int | None  # the forced value, or None: an X source
    inputs: tuple[tuple[str, int], ...]  # (pin, net) of each input pin


@dataclass(frozen=True)
class NonscanSetup:
    flops: tuple[NonscanFlop, ...]
    holds: dict[str, int]  # held input port -> value
    globs: tuple[str, ...]  # [scan] nonscan_cells
    release_faults: frozenset[tuple[str, str]]  # (fault site key, "sa0"/"sa1")

    @property
    def x_sources(self) -> tuple[int, ...]:
        return tuple(f.q for f in self.flops if f.value is None)

    def owns(self, cell: str) -> bool:
        """Whether ``cell`` is part of the non-scan logic: a match for a glob."""
        return any(fnmatch.fnmatchcase(cell, glob) for glob in self.globs)


def nonscan_instances(manifest: Mapping[str, Any]) -> list[str]:
    """The flops the stitcher left out of scan by policy, from its manifest."""
    return sorted(
        str(item["instance"])
        for item in manifest.get("ineligible_ffs", [])
        if isinstance(item, dict) and item.get("reason") == NONSCAN_REASON
    )


def _check_holds(
    module: Mapping[str, Any],
    manifest: Mapping[str, Any],
    holds: Mapping[str, int],
    scan_reset_holds: Mapping[str, bool],
) -> None:
    inputs = dict(_port_bits(module, "input"))
    ports = module.get("ports", {})
    scan_ports = {
        str(manifest.get("scan_enable", "")),
        *(str(p) for p in manifest.get("scan_inputs", [])),
    }
    scan_clocks = set(manifest_clock_net_ids(dict(manifest)))
    for name, value in holds.items():
        port = ports.get(name)
        if (
            not isinstance(port, dict)
            or port.get("direction") != "input"
            or name not in inputs
        ):
            raise ScanError(f"[scan] hold {name}: not a single-bit input port")
        if name in scan_ports:
            raise ScanError(f"[scan] hold {name}: a scan port can't be held")
        if inputs[name] in scan_clocks:
            raise ScanError(f"[scan] hold {name}: a scan clock can't be held")
        if name in scan_reset_holds and value != int(scan_reset_holds[name]):
            raise ScanError(
                f"[scan] hold {name}:{value} would clear or preset scan flops, "
                f"which need it at {int(scan_reset_holds[name])} during the test"
            )


def analyze_nonscan(
    module: Mapping[str, Any],
    manifest: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    *,
    globs: Sequence[str],
    holds: Sequence[tuple[str, int]],
    scan_reset_holds: Mapping[str, bool],
) -> NonscanSetup:
    """Classify every non-scan flop of the stitched ``module`` (see the module
    docstring), or raise :class:`ScanError`."""
    held = dict(holds)
    _check_holds(module, manifest, held, scan_reset_holds)
    wanted = set(nonscan_instances(manifest))
    netlist = _Netlist(module, cell_map)
    try:
        flops = {flop.instance: flop for flop in netlist.flops()}
    except JtagSetupError as exc:
        raise ScanError(str(exc)) from exc
    missing = sorted(wanted - set(flops))
    if missing:
        raise ScanError(f"non-scan flops not in the scanned netlist: {missing}")

    result: list[NonscanFlop] = []
    release: set[tuple[str, str]] = set()
    for instance in sorted(wanted):
        flop = flops[instance]
        entry = netlist.entry(instance) or {}
        ff = entry.get("ff", {})
        forcing = []
        for control in flop.controls:
            value = _held_value(control.trace, held)
            if value is None:
                raise ScanError(
                    f"non-scan flop {instance}: its {control.kind} isn't held, so a "
                    "scan pattern could change it; hold the input that drives it"
                )
            if value == control.active:
                forcing.append(control)
        if len(forcing) > 1:
            raise ScanError(
                f"non-scan flop {instance}: clear and preset both held active"
            )
        cell = netlist.cells[instance]
        directions = {
            pin: netlist.direction(instance, pin, entry) for pin in cell["connections"]
        }
        inputs = tuple(
            (pin, bits[0])
            for pin, bits in sorted(cell["connections"].items())
            if directions[pin] == "input"
            and len(bits) == 1
            and isinstance(bits[0], int)
        )
        if forcing:
            control = forcing[0]
            # What the simulator forces: the cell map's value, 0 if it names none.
            forced = int(ff.get(control.kind, {}).get("value", 0))
            release |= _reset_path_faults(control)
            result.append(NonscanFlop(instance, flop.q, forced, inputs))
            continue
        if _held_value(flop.clock, held) is None:
            raise ScanError(
                f"non-scan flop {instance}: no held input keeps it in reset and its "
                "clock isn't held, so it would change during a scan test"
            )
        result.append(NonscanFlop(instance, flop.q, None, inputs))
    return NonscanSetup(
        flops=tuple(result),
        holds=held,
        globs=tuple(globs),
        release_faults=frozenset(release),
    )


def jtag_sites(
    generic_rows: Sequence[Mapping[str, Any]],
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    setup: NonscanSetup,
    *,
    tdo: str | None,
    blackbox_instances: Sequence[str] = (),
) -> set[str]:
    """The fault sites of the stitched ``module`` that scan leaves to JTAG (exclusion
    ``jtag``, graded by ``ff.py jtag``):

    1. the non-scan logic's own: a branch into a cell a glob matches, or a stem
       such a cell drives -- unless it reaches nothing at all, which scan proves
       redundant;
    2. what only the non-scan logic sees: a site whose every path through
       combinational logic ends at a non-scan flop's pin or at ``tdo`` -- none at a
       scan flop, another output or a blackbox -- like a TAP's own inputs and the
       decode that selects its network.
    """
    netlist = _Netlist(module, cell_map)
    nonscan_flops = {flop.instance for flop in setup.flops}
    boxes = set(blackbox_instances)
    readers: dict[int, list[str]] = {}
    for instance, cell in netlist.cells.items():
        if instance in boxes:
            continue
        entry = netlist.entry(instance)
        for pin, bits in cell.get("connections", {}).items():
            if netlist.direction(instance, pin, entry) == "output":
                continue
            for bit in bits:
                if isinstance(bit, int):
                    readers.setdefault(bit, []).append(instance)

    def passes_through(instance: str) -> bool:
        """Combinational: its inputs reach whatever its outputs do."""
        if instance in boxes or instance in nonscan_flops:
            return False
        entry = netlist.entry(instance)
        if entry is None:
            directions = netlist.cells[instance].get("port_directions")
            return isinstance(directions, dict) and bool(directions)
        return entry.get("node_type") not in ("FF", "LATCH")

    tdo_bits: set[int] = set()
    observed: set[int] = set()  # nets a scan test can see through some path
    only_jtag_seeds: set[int] = set()
    for name, port in module.get("ports", {}).items():
        if isinstance(port, dict) and port.get("direction") == "output":
            bits = {b for b in port.get("bits", []) if isinstance(b, int)}
            (tdo_bits if name == tdo else observed).update(bits)
    only_jtag_seeds |= tdo_bits
    for net, cells in readers.items():
        for instance in cells:
            if instance in nonscan_flops:
                only_jtag_seeds.add(net)
            elif not passes_through(instance):
                observed.add(net)
    for instance in boxes:
        for pin, bits in netlist.cells.get(instance, {}).get("connections", {}).items():
            directions = netlist.cells[instance].get("port_directions", {})
            if directions.get(pin) != "output":
                observed.update(b for b in bits if isinstance(b, int))

    def closure(seeds: set[int]) -> set[int]:
        """Every net with a combinational path to a seed."""
        reached = set(seeds)
        work = list(seeds)
        while work:
            net = work.pop()
            driver = netlist.drivers.get(net)
            if driver is None or not passes_through(driver[0]):
                continue
            cell = netlist.cells[driver[0]]
            entry = netlist.entry(driver[0])
            for pin, bits in cell.get("connections", {}).items():
                if netlist.direction(driver[0], pin, entry) == "output":
                    continue
                for bit in bits:
                    if isinstance(bit, int) and bit not in reached:
                        reached.add(bit)
                        work.append(bit)
        return reached

    seen = closure(observed)
    jtag_seen = closure(only_jtag_seeds)

    def outputs_of(instance: str) -> list[int]:
        cell = netlist.cells.get(instance, {})
        entry = netlist.entry(instance) if instance in netlist.cells else None
        return [
            bit
            for pin, bits in cell.get("connections", {}).items()
            if netlist.direction(instance, pin, entry) == "output"
            for bit in bits
            if isinstance(bit, int)
        ]

    # Logic that reaches nothing at all -- an unread net -- stays scan's, whoever
    # owns it: SAT proves it redundant, which no JTAG program could.
    sites: set[str] = set()
    for row in generic_rows:
        key = str(row["site_key"])
        net = int(row["yosys_net_id"])
        if str(row["kind"]) == "branch":
            consumer = str(row.get("consumer_instance", ""))
            if consumer in nonscan_flops:
                sites.add(key)
            elif consumer in netlist.cells and passes_through(consumer):
                outs = outputs_of(consumer)
                to_jtag = any(o in jtag_seen for o in outs)
                to_scan = any(o in seen for o in outs)
                if (setup.owns(consumer) and (to_jtag or to_scan)) or (
                    to_jtag and not to_scan
                ):
                    sites.add(key)
            elif setup.owns(consumer):
                sites.add(key)
            continue
        driver = netlist.drivers.get(net)
        live = net in seen or net in jtag_seen
        if driver is not None and setup.owns(driver[0]) and live:
            sites.add(key)
        elif net in jtag_seen and net not in seen:
            sites.add(key)
    return sites
