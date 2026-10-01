"""Non-scan cells in a scan test: ``[scan] nonscan_cells`` and ``[scan] hold``.

A flop whose instance matches a ``nonscan_cells`` glob stays out of the scan chains (the
stitcher records it as ineligible, reason ``nonscan_policy``) -- a JTAG TAP and its
IJTAG network, run non-scan and tested through TCK by ``ff.py jtag``. The scan view is
one combinational frame between load and unload, so such a flop must hold still for the
whole test. Each must be one of:

- *forced*: a held input or a constant keeps its clear or preset active -- traced
  through buffers, inverters and gates a held input sets at its controlling value
  (:mod:`faultflow.control_trace`), like a TDR bit cleared through
  ``trst_n & clr_n`` with ``trst_n`` held at 0. Its output is the value that control
  forces, and the view ties it there (``$faultflow_tie0``/``$faultflow_tie1``).
- *frozen*: its clock is held or constant and no free input can clear or preset it.
  Its value is unknown, so its output is an X source, masked like an unknown blackbox
  output.
- *settled*: a scan clock keeps clocking it, the holds keep its clear and preset
  inactive, and its D traces to holds, constants and other settled flops -- a reset
  synchronizer with the chip reset held inactive. It reaches its D's value a known
  number of clock pulses into the test, and the view ties it there; every pattern
  starts with that many pulses, its *preamble* (:attr:`NonscanSetup.preamble`). Its
  own fault sites are reset faults (:func:`settled_sites`): scan can't test them, and
  they are the reset of what it feeds.

Anything else refuses. ``[scan] hold`` inputs are held in every cycle of every scan
pattern; the view ties them (their port removed), so ATPG can't assign them either. A
hold must be a single-bit input, can't be a scan port or a scan clock, and can't put a
scan flop's clear or preset in its active level.

A stuck-at on a forcing control's traced path can release a forced flop in the faulty
machine, which a tie can't show: :attr:`NonscanSetup.release_faults` lists those (site
key, fault type) pairs, which scan leaves to JTAG. A glob may select settled flops or
others, not both: the logic a glob selects beside JTAG's flops is JTAG's.

A fault the ties alone make untestable -- glue logic a TDR held at its reset value
gates, say -- is hold_unresolved, not redundant: SAT finds a test on the view's hold
twin (atpg_view.make_nonscan_free), where they are free.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.control_trace import (
    Flop,
    Netlist,
    TraceError,
    port_bits,
    release_faults,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.manifest import manifest_clock_net_ids

NONSCAN_REASON = "nonscan_policy"
# The nets the C++ core gives a constant bit (common/types.hpp; x and z read as 0).
CONST_NETS = {"0": -1, "x": -1, "z": -1, "1": -2}


Pins = tuple[tuple[str, int], ...]  # (pin, net)


@dataclass(frozen=True)
class NonscanFlop:
    instance: str
    q: int  # its output net
    value: int | None  # the value the view ties it to, or None: an X source
    inputs: tuple[tuple[str, int], ...]  # (pin, net) of each input pin
    settle: int = 0  # settled: scan-clock pulses until it holds `value`
    tied: tuple[tuple[str, int], ...] = ()  # (pin, constant's net) of each tied pin


@dataclass(frozen=True)
class NonscanSetup:
    flops: tuple[NonscanFlop, ...]
    holds: dict[str, int]  # held input port -> value
    globs: tuple[str, ...]  # [scan] nonscan_cells
    release_faults: frozenset[tuple[str, str]]  # (fault site key, "sa0"/"sa1")

    @property
    def x_sources(self) -> tuple[int, ...]:
        return tuple(f.q for f in self.flops if f.value is None)

    @property
    def settled(self) -> tuple[NonscanFlop, ...]:
        return tuple(f for f in self.flops if f.settle)

    @property
    def preamble(self) -> int:
        """The scan-clock pulses every pattern starts with: the settled flops'
        longest settling."""
        return max((f.settle for f in self.flops), default=0)

    def owns(self, cell: str) -> bool:
        """Whether ``cell`` is part of the non-scan logic JTAG tests: a match for a
        glob that selects no settled flop."""
        settled = {f.instance for f in self.settled}
        if cell in settled:
            return False
        return any(
            fnmatch.fnmatchcase(cell, glob)
            for glob in self.globs
            if not any(fnmatch.fnmatchcase(s, glob) for s in settled)
        )


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
    inputs = dict(port_bits(module, "input"))
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
    netlist = Netlist(module, cell_map)
    try:
        flops = {flop.instance: flop for flop in netlist.flops()}
    except TraceError as exc:
        raise ScanError(str(exc)) from exc
    missing = sorted(wanted - set(flops))
    if missing:
        raise ScanError(f"non-scan flops not in the scanned netlist: {missing}")

    result: list[NonscanFlop] = []
    release: set[tuple[str, str]] = set()
    unsettled: list[tuple[str, Pins, Pins]] = []
    for instance in sorted(wanted):
        flop = flops[instance]
        entry = netlist.entry(instance) or {}
        forcing = []
        for control in flop.controls:
            found = netlist.forced(instance, control.pin, held)
            if found is None:
                raise ScanError(
                    f"non-scan flop {instance}: its {control.kind} isn't held, so a "
                    "scan pattern could change it; hold the input that drives it"
                )
            if found.value == control.active:
                forcing.append((control, found))
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
        tied = tuple(
            (pin, CONST_NETS[bits[0]])
            for pin, bits in sorted(cell["connections"].items())
            if directions[pin] == "input" and len(bits) == 1 and bits[0] in CONST_NETS
        )
        if forcing:
            control, found = forcing[0]
            release |= release_faults(found)
            result.append(
                NonscanFlop(instance, flop.q, control.value, inputs, tied=tied)
            )
            continue
        if netlist.forced(instance, flop.clock_pin, held) is None:
            unsettled.append((instance, inputs, tied))
            continue
        result.append(NonscanFlop(instance, flop.q, None, inputs, tied=tied))
    result += _settle(netlist, flops, unsettled, held, manifest)
    _check_globs(globs, result)
    return NonscanSetup(
        flops=tuple(sorted(result, key=lambda f: f.instance)),
        holds=held,
        globs=tuple(globs),
        release_faults=frozenset(release),
    )


def _settle(
    netlist: Netlist,
    flops: Mapping[str, Flop],
    candidates: list[tuple[str, Pins, Pins]],
    held: Mapping[str, int],
    manifest: Mapping[str, Any],
) -> list[NonscanFlop]:
    """The settled flops among ``candidates`` -- non-scan flops nothing holds still
    -- by induction: a plain flop a scan clock clocks, whose D is held (by the holds,
    a constant or flops settled already), settles one pulse after its D does.
    Refuses any other."""
    scan_clocks = set(manifest_clock_net_ids(dict(manifest)))
    settled: dict[int, int] = {}  # q net -> value
    depth: dict[int, int] = {}  # q net -> pulses to settle
    found: list[NonscanFlop] = []
    pending = list(candidates)
    while pending:
        progress = False
        for instance, inputs, tied in list(pending):
            flop = flops[instance]
            ff = (netlist.entry(instance) or {}).get("ff", {})
            clock = flop.clock.steps[-1][0] if flop.clock.steps else None
            if flop.clock.port is None or clock not in scan_clocks:
                continue
            if "enable" in ff or "scan" in ff:
                continue
            data = netlist.forced(instance, str(ff.get("data", "D")), held, settled)
            if data is None:
                continue
            settle = 1 + max(
                (depth[step[0]] for step in data.steps if step[0] in depth), default=0
            )
            settled[flop.q], depth[flop.q] = data.value, settle
            found.append(
                NonscanFlop(instance, flop.q, data.value, inputs, settle, tied)
            )
            pending.remove((instance, inputs, tied))
            progress = True
        if not progress:
            raise ScanError(
                f"non-scan flop {pending[0][0]}: no held input keeps it in reset and "
                "its clock isn't held, so it would change during a scan test -- "
                "unless a scan clock settles it: a plain flop whose D traces to "
                "holds, constants and other settled flops"
            )
    return found


def _check_globs(globs: Sequence[str], flops: Sequence[NonscanFlop]) -> None:
    """A glob selects settled flops or others, not both (the logic beside the
    others is JTAG's: NonscanSetup.owns)."""
    for glob in globs:
        hits = [f for f in flops if fnmatch.fnmatchcase(f.instance, glob)]
        settled = sorted(f.instance for f in hits if f.settle)
        other = sorted(f.instance for f in hits if not f.settle)
        if settled and other:
            raise ScanError(
                f"[scan] nonscan_cells {glob} selects flops a scan clock settles "
                f"({settled[0]}) and flops it doesn't ({other[0]}); give each its "
                "own glob"
            )


def settled_sites(
    generic_rows: Sequence[Mapping[str, Any]], setup: NonscanSetup
) -> set[str]:
    """The settled flops' own fault sites: each output's stem, and each input pin,
    tied to a constant or not -- its branch, or the net's stem where the pin is the
    net's only reader. They
    are reset faults (``excluded_reset``): scan can't test them -- the flops settle
    before the test, outside it -- and they reset what they feed."""
    if not setup.settled:
        return set()
    branches: dict[tuple[int, str, str], str] = {}
    stems: dict[int, str] = {}
    for row in generic_rows:
        net = int(row["yosys_net_id"])
        if str(row["kind"]) == "branch":
            consumer = str(row.get("consumer_instance", ""))
            key = (net, consumer, str(row.get("input_pin", "")))
            branches[key] = str(row["site_key"])
        else:
            stems[net] = str(row["site_key"])
    branched = {net for net, _, _ in branches}
    sites: set[str] = set()
    for flop in setup.settled:
        if flop.q in stems:
            sites.add(stems[flop.q])
        for pin, net in flop.inputs + flop.tied:
            branch = branches.get((net, flop.instance, pin))
            if branch is not None:
                sites.add(branch)
            elif net not in branched and net in stems:
                sites.add(stems[net])
    return sites


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
    ``jtag``, graded by ``ff.py jtag``) -- settled flops, and their globs, aside:

    1. the non-scan logic's own: a branch into a cell a glob matches, or a stem
       such a cell drives -- unless it reaches nothing at all, which scan proves
       redundant;
    2. what only the non-scan logic sees: a site whose every path through
       combinational logic ends at a non-scan flop's pin or at ``tdo`` -- none at a
       scan flop, another output or a blackbox -- like a TAP's own inputs and the
       decode that selects its network.
    """
    netlist = Netlist(module, cell_map)
    nonscan_flops = {flop.instance for flop in setup.flops if not flop.settle}
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
