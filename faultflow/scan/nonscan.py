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
- *programmed*: the test setup program (``[scan] test_setup``,
  :mod:`faultflow.scan.setup`) leaves it at a value, from any power-up state
  (:mod:`faultflow.scan.setup_sim`), and it holds that value through the test: the
  holds keep its clear and preset inactive, and its clock is held, or its D traces
  back to that value -- its own output through a mux a held input selects, say --
  or its enable is held off; each of these with the other programmed flops at their
  values. The view ties it there, like a forced flop.
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

An IEEE 1500 WSP's flops (:mod:`faultflow.wrap.wsp`) stay out of scan too (reason
``wsp``): the program that loads the test mode's instruction programs them
(:mod:`faultflow.wrap.wsp_setup`).

Anything else refuses. ``[scan] hold`` inputs are held in every cycle of every scan
pattern; the view ties them (their port removed), so ATPG can't assign them either. A
hold must be a single-bit input, can't be a scan port or a scan clock, and can't put a
scan flop's clear or preset in its active level.

A stuck-at on a forcing control's traced path can release a forced flop in the faulty
machine, which a tie can't show, and so can one on a path proving a programmed flop
holds; and the setup program runs fault-free in grading, so a stuck-at that changes
what it leaves in a programmed flop (on silicon, the program runs with the fault)
can't be credited either. :attr:`NonscanSetup.release_faults` lists all those (site
key, fault type) pairs, which scan leaves to JTAG. A glob may select settled flops or
others, not both: the logic a glob selects beside JTAG's flops is JTAG's.

A fault the ties alone make untestable -- glue logic a TDR held at its reset value
gates, say -- is hold_unresolved, not redundant: SAT finds a test on the view's hold
twin (atpg_view.make_nonscan_free), where they are free.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping, Sequence

from faultflow.control_trace import (
    Flop,
    Forced,
    Netlist,
    TraceError,
    port_bits,
    release_faults,
)
from faultflow.coverage.site_key import SiteProvenance, canonical_site_key
from faultflow.scan.errors import ScanError
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.setup import SetupCycle, check_setup
from faultflow.scan.setup_sim import SetupSimulator, changed_by

NONSCAN_REASON = "nonscan_policy"
WSP_REASON = "wsp"  # an IEEE 1500 WSP's flop: its mode's program programs it
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
    programmed: bool = False  # `value` is what the test setup program leaves


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
    def programmed(self) -> tuple[NonscanFlop, ...]:
        return tuple(f for f in self.flops if f.programmed)

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
    """The flops the stitcher left out of scan by policy, or as an IEEE 1500
    WSP's, from its manifest."""
    return sorted(
        str(item["instance"])
        for item in manifest.get("ineligible_ffs", [])
        if isinstance(item, dict) and item.get("reason") in (NONSCAN_REASON, WSP_REASON)
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
    setup: Sequence[SetupCycle] = (),
    blackbox_instances: Sequence[str] = (),
) -> NonscanSetup:
    """Classify every non-scan flop of the stitched ``module`` (see the module
    docstring), or raise :class:`ScanError`. ``setup``: the test setup program."""
    held = dict(holds)
    _check_holds(module, manifest, held, scan_reset_holds)
    clock_off = scan_clock_off(module, manifest)
    check_setup(
        setup, [name for name, _ in port_bits(module, "input")], list(clock_off)
    )
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
    rest: list[tuple[str, Pins, Pins]] = []  # neither forced
    forced_values: dict[int, int] = {}
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
            forced_values[flop.q] = control.value
            result.append(
                NonscanFlop(instance, flop.q, control.value, inputs, tied=tied)
            )
            continue
        rest.append((instance, inputs, tied))
    programmed, faults = _program(
        netlist,
        flops,
        [instance for instance, _, _ in rest],
        held,
        forced_values,
        _Program(setup, clock_off, tuple(globs), tuple(blackbox_instances)),
    )
    release |= faults
    unsettled: list[tuple[str, Pins, Pins]] = []
    for instance, inputs, tied in rest:
        flop = flops[instance]
        if instance in programmed:
            value = programmed[instance]
            result.append(
                NonscanFlop(instance, flop.q, value, inputs, tied=tied, programmed=True)
            )
            continue
        if netlist.forced(instance, flop.clock_pin, held) is None:
            unsettled.append((instance, inputs, tied))
            continue
        result.append(NonscanFlop(instance, flop.q, None, inputs, tied=tied))
    known = {flops[instance].q: value for instance, value in programmed.items()}
    result += _settle(netlist, flops, unsettled, held, manifest, known)
    _check_globs(globs, result)
    return NonscanSetup(
        flops=tuple(sorted(result, key=lambda f: f.instance)),
        holds=held,
        globs=tuple(globs),
        release_faults=frozenset(release),
    )


def scan_clock_off(
    module: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, int]:
    """Each scan clock input bit of ``module``, by name, and its off level: 0, as
    FaultFlow grades and a tester applies (tester_program)."""
    clocks = set(manifest_clock_net_ids(dict(manifest)))
    return {name: 0 for name, net in port_bits(module, "input") if net in clocks}


@dataclass(frozen=True)
class _Program:
    """What the programmed class is found with: the test setup program, the scan
    clocks' off levels, the globs (JTAG's logic) and the blackboxes."""

    cycles: Sequence[SetupCycle]
    clock_off: Mapping[str, int]
    globs: tuple[str, ...]
    blackbox_instances: tuple[str, ...]


def _program(
    netlist: Netlist,
    flops: Mapping[str, Flop],
    candidates: Sequence[str],
    held: Mapping[str, int],
    forced: Mapping[int, int],
    program: _Program,
) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """The programmed flops among ``candidates`` (non-scan flops not forced), each
    with its value, and the faults scan must leave to JTAG for them: those that
    could make one let go of its value during the test (on its proof's paths), and
    those that change what the program leaves in one. The proofs assume every
    programmed flop at its value -- and drop any flop whose proof fails until all
    hold (the largest such set). ``forced``: the forced flops' outputs, at their
    values."""
    if not program.cycles or not candidates:
        return {}, set()
    sim = SetupSimulator(netlist, candidates, program.blackbox_instances)
    values, known_nets = sim.run(program.cycles, program.clock_off, held)
    programmed = {name: v for name, v in values.items() if v is not None}
    proofs: dict[str, list[Forced] | None] = {}
    while True:
        known = {**forced, **{flops[name].q: v for name, v in programmed.items()}}
        proofs = {
            name: _holds(netlist, flops[name], v, held, known)
            for name, v in programmed.items()
        }
        failed = [name for name, proof in proofs.items() if proof is None]
        if not failed:
            break
        for name in failed:
            del programmed[name]
    faults: set[tuple[str, str]] = set()
    for proof in proofs.values():
        for found in proof or ():
            faults |= release_faults(found)
    if programmed:
        faults |= _setup_faults(sim, netlist, program, held, programmed, known_nets)
    return programmed, faults


def _holds(
    netlist: Netlist,
    flop: Flop,
    value: int,
    held: Mapping[str, int],
    known: Mapping[int, int],
) -> list[Forced] | None:
    """The paths proving ``flop`` keeps ``value`` through a scan test -- the holds
    at their values, the flops in ``known`` at theirs -- or None: no clear or preset
    active but at ``value``, and its clock held, its enable held off, or its D held
    at ``value`` (its scan enable held off first, for a scan cell)."""
    instance = flop.instance
    proof: list[Forced] = []
    active = []
    for control in flop.controls:
        found = netlist.forced(instance, control.pin, held, known)
        if found is None:
            return None
        proof.append(found)
        if found.value == control.active:
            active.append(control)
    if active:
        return proof if len(active) == 1 and active[0].value == value else None
    clock = netlist.forced(instance, flop.clock_pin, held, known)
    if clock is not None:
        return proof + [clock]
    ff = (netlist.entry(instance) or {}).get("ff", {})
    scan = ff.get("scan")
    if isinstance(scan, dict):
        on = 0 if str(scan.get("enable_polarity", "HIGH")).upper() == "LOW" else 1
        enable = netlist.forced(instance, str(scan["enable"]), held, known)
        if enable is None or enable.value == on:
            return None
        proof.append(enable)
    data_enable = ff.get("enable")
    if isinstance(data_enable, dict):
        on = 0 if str(data_enable.get("level", "HIGH")).upper() == "LOW" else 1
        enable = netlist.forced(instance, str(data_enable["pin"]), held, known)
        if enable is not None and enable.value != on:
            return proof + [enable]
    data = netlist.forced(instance, str(ff.get("data", "D")), held, known)
    if data is not None and data.value == value:
        return proof + [data]
    return None


def _setup_faults(
    sim: SetupSimulator,
    netlist: Netlist,
    program: _Program,
    held: Mapping[str, int],
    programmed: Mapping[str, int],
    known_nets: set[int],
) -> set[tuple[str, str]]:
    """The stuck-ats that change what the program leaves in a programmed flop --
    to the other value or unknown -- outside the logic the globs select (JTAG's
    already). Only a net the program sets at some step can: a stuck-at on a net
    unknown throughout narrows an unknown, and the three-valued run is monotone."""

    def owned(instance: str) -> bool:
        return any(fnmatch.fnmatchcase(instance, glob) for glob in program.globs)

    readers: dict[int, set[tuple[str, str]]] = {}
    for gate in sim.gates:
        for pin, bit in gate.inputs:
            if isinstance(bit, int):
                readers.setdefault(bit, set()).add((gate.instance, pin))
    for cone_flop in sim.flops:
        for pin, bit in SetupSimulator.flop_pins(cone_flop):
            if isinstance(bit, int):
                readers.setdefault(bit, set()).add((cone_flop.instance, pin))
    faults: set[tuple[str, str]] = set()
    for net in sorted(known_nets):
        driver = netlist.drivers.get(net)
        sites: list[tuple[str | None, str | None]] = []
        if driver is None or not owned(driver[0]):
            sites.append((None, None))
        sites += [
            (consumer, pin)
            for consumer, pin in sorted(readers.get(net, set()))
            if not owned(consumer)
        ]
        for reader, reader_pin in sites:
            if reader is None:
                where = SiteProvenance(yosys_net_id=net, kind="stem")
            else:
                where = SiteProvenance(
                    yosys_net_id=net,
                    kind="branch",
                    consumer_instance=reader,
                    input_pin=str(reader_pin),
                )
            for stuck in (0, 1):
                fault = (net, reader, reader_pin, stuck)
                if changed_by(
                    sim, program.cycles, program.clock_off, held, programmed, fault
                ):
                    faults.add((canonical_site_key(where), f"sa{stuck}"))
    return faults


def _settle(
    netlist: Netlist,
    flops: Mapping[str, Flop],
    candidates: list[tuple[str, Pins, Pins]],
    held: Mapping[str, int],
    manifest: Mapping[str, Any],
    known: Mapping[int, int] | None = None,
) -> list[NonscanFlop]:
    """The settled flops among ``candidates`` -- non-scan flops nothing holds still
    -- by induction: a plain flop a scan clock clocks, whose D is held (by the holds,
    a constant, a programmed flop in ``known`` or flops settled already), settles one
    pulse after its D does. Refuses any other."""
    scan_clocks = set(manifest_clock_net_ids(dict(manifest)))
    settled: dict[int, int] = dict(known or {})  # q net -> value
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
                "holds, constants and other settled flops; or the test setup "
                "program ([scan] test_setup) leaves it at a value it then holds"
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
    tap_inputs: Sequence[str] = (),
) -> set[str]:
    """The fault sites of the stitched ``module`` that scan leaves to JTAG (exclusion
    ``jtag``, graded by ``ff.py jtag``) -- settled flops, and their globs, aside:

    1. the non-scan logic's own: a branch into a cell a glob matches, or a stem
       such a cell drives -- unless it reaches nothing at all, which scan proves
       redundant;
    2. what only the non-scan logic sees: a site whose every path through
       combinational logic ends at a non-scan flop's pin or at ``tdo`` -- none at a
       scan flop, another output or a blackbox -- like a TAP's own inputs and the
       decode that selects its network;
    3. what only the non-scan logic drives: a net whose every source is such a
       flop's output, one of the ``tap_inputs`` (port names) or a constant -- a TDR
       bit, or the buffer or gate that carries it on -- is the test-access path:
       its stem and each of its branches, down to the D pin of the first scan flop
       on the other side. A gate that mixes in any other signal drives a net that
       is the core's, though the pin the JTAG side enters by is not.
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

    owned_flops = {name for name in nonscan_flops if setup.owns(name)}
    driven = _jtag_driven_nets(
        netlist,
        module,
        owned_flops,
        tap_inputs if owned_flops else (),
        passes_through,
    )

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
            elif net in driven and consumer in netlist.cells:
                # The access path: a JTAG-driven net's branch into anything that
                # reads it, unless what it feeds reaches nothing.
                if not passes_through(consumer) or any(
                    o in seen or o in jtag_seen for o in outputs_of(consumer)
                ):
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
        elif net in driven and live:
            sites.add(key)
    return sites


_JTAG, _CONST, _OTHER = "jtag", "const", "other"


def _jtag_driven_nets(
    netlist: Netlist,
    module: Mapping[str, Any],
    owned_flops: Collection[str],
    tap_inputs: Sequence[str],
    passes_through: Callable[[str], bool],
) -> set[int]:
    """The nets only the JTAG logic drives: the outputs of the ``owned_flops``, the
    ``tap_inputs`` (port names), and what combinational logic makes of them and of
    constants -- every source in a net's fan-in is one of those, and at least one is
    not a constant. A gate with any other input, a core signal, a scan flop's output
    or a blackbox's, drives a net that isn't."""
    if not owned_flops:
        return set()
    tap_nets = {net for name, net in port_bits(module, "input") if name in tap_inputs}
    state: dict[int, str] = {}
    visiting: set[int] = set()
    for root in sorted(netlist.drivers):
        if root in state:
            continue
        stack = [root]
        while stack:
            net = stack[-1]
            if net in state:
                stack.pop()
                continue
            driver = netlist.drivers.get(net)
            if driver is None:
                state[net] = _JTAG if net in tap_nets else _OTHER
                stack.pop()
                continue
            instance = driver[0]
            if instance in owned_flops:
                state[net] = _JTAG
                stack.pop()
                continue
            if not passes_through(instance):
                state[net] = _OTHER
                stack.pop()
                continue
            cell = netlist.cells[instance]
            entry = netlist.entry(instance)
            inputs = [
                bit
                for pin, bits in cell.get("connections", {}).items()
                if netlist.direction(instance, pin, entry) != "output"
                for bit in bits
                if isinstance(bit, int)
            ]
            pending = [bit for bit in inputs if bit not in state]
            if pending:
                if net in visiting:  # a combinational loop
                    state[net] = _OTHER
                    stack.pop()
                    continue
                visiting.add(net)
                stack.extend(pending)
                continue
            kinds = {state[bit] for bit in inputs}
            if _OTHER in kinds:
                state[net] = _OTHER
            elif _JTAG in kinds:
                state[net] = _JTAG
            else:
                state[net] = _CONST
            stack.pop()
    return {net for net, kind in state.items() if kind == _JTAG}
