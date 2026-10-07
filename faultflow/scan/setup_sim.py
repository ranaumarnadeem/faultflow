"""Three-valued simulation of a test setup program (:mod:`faultflow.scan.setup`): what
it leaves in the non-scan flops, whatever state the chip powered up in.

The C++ core is two-valued, by design, and grades a pattern from every flop at 0. A
flop a setup program loads may be tied in the scan view only if the program leaves it
the same value from any power-up state, which no two-valued run from one state shows.
This runs the program with every flop unknown (X) at the start, over the cells that can
reach the flops asked about, step for step as the core runs it (``setup_test_cycles``,
``scan_pattern_sim.cpp``), and reads each flop at the end: known, or X.

A gate's output is known when it is the same for every value of its unknown inputs, by
the core's own gate function (``eval_gate``); a flop's next state likewise, over every
value of its unknown clock levels, clear, preset, enable, scan enable and inputs, by
the core's flop update (``golden_ref_sim.cpp``). That is the usual pessimistic
three-valued simulation: it may call a value X that a closer look would find known,
never the reverse. A literal x or z on a pin is 0, as the core ties it.

A run can carry one stuck-at, active in every step: whether a fault changes what the
program leaves (:func:`changed_by`).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Mapping, Sequence

from faultflow.control_trace import Netlist, one_net
from faultflow.scan.errors import ScanError
from faultflow.scan.setup import SetupCycle, setup_test_cycles

Value = int | None  # 0, 1, or None: unknown

# A stuck-at: (net, the cell reading it and that cell's pin -- a branch -- or None and
# None for the net's stem, value).
Stuck = tuple[int, str | None, str | None, int]

# Bits a literal pin connection carries (the core ties x and z to 0).
_CONSTANTS = {"0": 0, "1": 1, "x": 0, "z": 0}

# An adder's outputs: each its own gate (graph_compiler.cpp, by the cell map's
# logical output name). The half adder's carry is AND2's formula (gate_eval.cpp),
# which eval_gate knows by that name.
_SPLIT = {
    ("ADDF", True): "ADDF",
    ("ADDF", False): "ADDF_CO",
    ("ADDH", True): "ADDH",
    ("ADDH", False): "AND2",
}


@lru_cache(maxsize=None)
def _eval(gate: str, bits: tuple[bool, ...]) -> int:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    if core is None:
        raise ScanError("the setup simulation needs the C++ core (eval_gate)")
    return int(core.eval_gate(gate, list(bits)))


def _join(values: Sequence[Value], fn: Callable[[tuple[int, ...]], int]) -> Value:
    """fn over every value of the unknowns in ``values``: its value if one, else X."""
    free = [i for i, v in enumerate(values) if v is None]
    found: set[int] = set()
    for assignment in itertools.product((0, 1), repeat=len(free)):
        given = dict(zip(free, assignment))
        bits = tuple(given[i] if v is None else int(v) for i, v in enumerate(values))
        found.add(fn(bits))
        if len(found) > 1:
            return None
    return found.pop()


Pin = tuple[str, Any]  # (pin, the bit on it: a net, or a literal "0"/"1"/"x"/"z")


@dataclass(frozen=True)
class _Gate:
    instance: str
    gate: str
    inputs: tuple[Pin, ...]
    output: int


@dataclass(frozen=True)
class _Control:
    pin: Pin
    active: int
    value: int


@dataclass(frozen=True)
class _Flop:
    instance: str
    q: int
    clock: Pin
    negedge: bool
    data: Pin
    clear: _Control | None
    preset: _Control | None
    conflict: int
    enable: tuple[Pin, int] | None  # (pin, active level)
    scan: tuple[Pin, Pin, int] | None  # (scan in, scan enable, active level)


def _active(spec: Mapping[str, Any]) -> int:
    return 0 if str(spec.get("level", "LOW")).upper() == "LOW" else 1


class SetupSimulator:
    """The cells of ``netlist`` that can reach ``targets`` (flop instances), ready to
    simulate setup programs on."""

    def __init__(
        self,
        netlist: Netlist,
        targets: Sequence[str],
        blackbox_instances: Sequence[str] = (),
    ) -> None:
        self.netlist = netlist
        self.targets = tuple(targets)
        boxes = set(blackbox_instances)
        gates: dict[int, _Gate] = {}
        flops: dict[str, _Flop] = {}
        self.x_sources: set[int] = set()
        work: list[int] = []
        seen: set[int] = set()

        def explore(pins: Sequence[Pin]) -> None:
            for _, bit in pins:
                if isinstance(bit, int) and bit not in seen:
                    seen.add(bit)
                    work.append(bit)

        for instance in self.targets:
            flop = self._flop(instance)
            flops[instance] = flop
            explore(self.flop_pins(flop))
        while work:
            net = work.pop()
            if net in netlist.input_net_names:
                continue
            driver = netlist.drivers.get(net)
            if driver is None or driver[0] in boxes:
                self.x_sources.add(net)
                continue
            instance, pin = driver
            entry = netlist.entry(instance)
            kind = entry.get("node_type") if entry is not None else None
            if entry is not None and kind in ("GATE", "CONST"):
                gate = self._gate(instance, pin, entry, net)
                gates[net] = gate
                explore(gate.inputs)
            elif entry is not None and kind == "FF":
                if instance not in flops:
                    flops[instance] = self._flop(instance)
                    explore(self.flop_pins(flops[instance]))
            else:
                self.x_sources.add(net)
        self.flops = tuple(sorted(flops.values(), key=lambda f: f.instance))
        self.gates = self._ordered(gates)
        # Every net the cone reads or drives.
        self.nets = seen | {f.q for f in self.flops}

    def _pin(self, instance: str, pin: str) -> Pin:
        conns = self.netlist.cells[instance].get("connections", {})
        bit = one_net(conns.get(pin))
        if bit is None:
            raise ScanError(f"{instance}.{pin} is not one bit")
        return pin, bit

    def _gate(
        self, instance: str, out_pin: str, entry: Mapping[str, Any], net: int
    ) -> _Gate:
        gate = str(entry.get("gate_type", ""))
        if gate in ("ADDF", "ADDH"):
            logical = {
                str(physical): str(name)
                for name, physical in entry.get("outputs", {}).items()
            }.get(out_pin, out_pin)
            gate = _SPLIT[(gate, logical in ("S", "YS"))]
        inputs = tuple(self._pin(instance, str(p)) for p in entry.get("inputs", []))
        return _Gate(instance, gate, inputs, net)

    def _flop(self, instance: str) -> _Flop:
        entry = self.netlist.entry(instance) or {}
        ff = entry.get("ff")
        if entry.get("node_type") != "FF" or not isinstance(ff, dict):
            raise ScanError(f"{instance} is not a flop the cell map knows")
        q = one_net(
            self.netlist.cells[instance]
            .get("connections", {})
            .get(str(ff.get("output", "Q")))
        )
        if not isinstance(q, int):
            raise ScanError(f"flop {instance} has no single-bit output net")

        def control(kind: str) -> _Control | None:
            spec = ff.get(kind)
            if not isinstance(spec, dict):
                return None
            return _Control(
                self._pin(instance, str(spec["pin"])),
                _active(spec),
                int(spec.get("value", 0 if kind == "clear" else 1)),
            )

        enable = ff.get("enable")
        scan = ff.get("scan")
        return _Flop(
            instance=instance,
            q=q,
            clock=self._pin(instance, str(ff.get("clock", "CLK"))),
            negedge=str(ff.get("trigger", "POSEDGE")).upper() == "NEGEDGE",
            data=self._pin(instance, str(ff.get("data", "D"))),
            clear=control("clear"),
            preset=control("preset"),
            conflict=int(ff.get("clear_preset_conflict_value", 0)),
            enable=(
                (self._pin(instance, str(enable["pin"])), _active(enable))
                if isinstance(enable, dict)
                else None
            ),
            scan=(
                (
                    self._pin(instance, str(scan["in"])),
                    self._pin(instance, str(scan["enable"])),
                    (
                        0
                        if str(scan.get("enable_polarity", "HIGH")).upper() == "LOW"
                        else 1
                    ),
                )
                if isinstance(scan, dict)
                else None
            ),
        )

    @staticmethod
    def flop_pins(flop: _Flop) -> list[Pin]:
        pins = [flop.clock, flop.data]
        for control in (flop.clear, flop.preset):
            if control is not None:
                pins.append(control.pin)
        if flop.enable is not None:
            pins.append(flop.enable[0])
        if flop.scan is not None:
            pins += [flop.scan[0], flop.scan[1]]
        return pins

    @staticmethod
    def _ordered(gates: Mapping[int, _Gate]) -> tuple[_Gate, ...]:
        """The gates, each after every gate driving one of its inputs."""
        pending = {
            net: {b for _, b in g.inputs if isinstance(b, int) and b in gates}
            for net, g in gates.items()
        }
        readers: dict[int, list[int]] = {}
        for net, needs in pending.items():
            for need in needs:
                readers.setdefault(need, []).append(net)
        ready = sorted(net for net, needs in pending.items() if not needs)
        order: list[_Gate] = []
        while ready:
            net = ready.pop()
            order.append(gates[net])
            for reader in readers.get(net, []):
                pending[reader].discard(net)
                if not pending[reader]:
                    ready.append(reader)
        if len(order) != len(gates):
            raise ScanError("the setup simulation found a combinational loop")
        return tuple(order)

    def run(
        self,
        cycles: Sequence[SetupCycle],
        clock_off: Mapping[str, int],
        final: Mapping[str, int],
        fault: Stuck | None = None,
    ) -> tuple[dict[str, Value], set[int]]:
        """Each target flop's value once ``cycles`` have run from an unknown state
        and one step more: the test's first, every scan clock off, each input in
        ``final`` (the test's held inputs) at its value and every other unknown.
        Also every net known at some step (a stuck-at on a net that never is can't
        change a known value). ``clock_off``: each scan clock's off level."""
        steps: list[tuple[dict[str, Value], dict[str, int]]] = [
            ({k: v for k, v in inputs.items()}, clocks)
            for inputs, clocks in setup_test_cycles(cycles, clock_off)
        ]
        held: dict[str, Value] = {
            name: None for name in self.netlist.input_net_names.values()
        }
        held.update({name: int(v) for name, v in final.items()})
        steps.append((held, dict(clock_off)))

        stem = branch = None
        if fault is not None:
            net, consumer, pin, stuck = fault
            if consumer is None:
                stem = (net, stuck)
            else:
                branch = (net, consumer, pin, stuck)

        values: dict[int, Value] = {}
        state: dict[str, Value] = {f.instance: None for f in self.flops}
        prev: dict[int, Value] | None = None  # the last step's values; none yet
        known: set[int] = set()
        names = self.netlist.input_net_names

        def read(pin: Pin, instance: str, at: Mapping[int, Value]) -> Value:
            name, bit = pin
            if isinstance(bit, str):
                return _CONSTANTS.get(bit, 0)
            if (
                branch is not None
                and bit == branch[0]
                and instance == branch[1]
                and name == branch[2]
            ):
                return branch[3]
            return at.get(bit)

        def put(net: int, value: Value) -> None:
            values[net] = stem[1] if stem is not None and net == stem[0] else value

        def settle() -> None:
            for flop in self.flops:
                put(flop.q, state[flop.instance])
            for gate in self.gates:
                if gate.gate == "CONST0":
                    put(gate.output, 0)
                elif gate.gate == "CONST1":
                    put(gate.output, 1)
                else:
                    ins = [read(pin, gate.instance, values) for pin in gate.inputs]
                    name = gate.gate
                    put(
                        gate.output,
                        _join(ins, lambda bits: _eval(name, tuple(map(bool, bits)))),
                    )
            known.update(net for net in self.nets if values.get(net) is not None)

        for inputs, clocks in steps:
            given = {**inputs, **clocks}
            for net, name in names.items():
                put(net, given.get(name, 0))
            for net in self.x_sources:
                put(net, None)
            settle()
            state = {
                f.instance: self._next(f, state, values, prev, read) for f in self.flops
            }
            settle()
            prev = dict(values)
        return {t: state[t] for t in self.targets}, known

    @staticmethod
    def _next(
        flop: _Flop,
        state: Mapping[str, Value],
        values: Mapping[int, Value],
        prev: Mapping[int, Value] | None,
        read: Callable[[Pin, str, Mapping[int, Value]], Value],
    ) -> Value:
        """The flop's next state: golden_ref_sim.cpp's update_ff_states over every
        value of its unknowns. Before the first step the core has every net at 0
        (run_sequence); a chip has them unknown, and so does this."""
        here = flop.instance
        operands: list[Value] = [
            None if prev is None else read(flop.clock, here, prev),
            read(flop.clock, here, values),
            read(flop.data, here, values),
            state[here],
            read(flop.clear.pin, here, values) if flop.clear else 0,
            read(flop.preset.pin, here, values) if flop.preset else 0,
            read(flop.enable[0], here, values) if flop.enable else 0,
            read(flop.scan[0], here, values) if flop.scan else 0,
            read(flop.scan[1], here, values) if flop.scan else 0,
        ]

        def update(bits: tuple[int, ...]) -> int:
            old, clock, data, q, clear, preset, enable, scan_in, scan_en = bits
            scanning = flop.scan is not None and scan_en == flop.scan[2]
            cleared = (
                not scanning and flop.clear is not None and clear == flop.clear.active
            )
            preset_on = (
                not scanning
                and flop.preset is not None
                and preset == flop.preset.active
            )
            if cleared and preset_on:
                return flop.conflict
            if cleared:
                return flop.clear.value if flop.clear else 0
            if preset_on:
                return flop.preset.value if flop.preset else 1
            edge = (old, clock) == ((1, 0) if flop.negedge else (0, 1))
            if not edge:
                return q
            if scanning:
                return scan_in
            if flop.enable is not None and enable != flop.enable[1]:
                return q
            return data

        return _join(operands, update)


def changed_by(
    sim: SetupSimulator,
    cycles: Sequence[SetupCycle],
    clock_off: Mapping[str, int],
    final: Mapping[str, int],
    expected: Mapping[str, Value],
    fault: Stuck,
) -> bool:
    """Whether ``fault``, active throughout, changes any target's value from
    ``expected`` (the fault-free run's) -- to the other value, or to unknown."""
    found, _ = sim.run(cycles, clock_off, final, fault)
    return any(found[t] != expected[t] for t in expected)
