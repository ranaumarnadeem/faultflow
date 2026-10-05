"""The continuous-flow decompressor of a composed chip, checked by what its synthesized
cells do: simulated symbolically over GF(2) through the cycles a tester applies, every
chain's scan-in at every shift must be exactly the model's
(:func:`faultflow.scan.ring_generator.continuous_scan_in_rows`) -- for every channel
stream at once, and from any power-up state.

A value is a Boolean function of a few *leaves*, each an affine form over the
variables: one per channel bit per shift, one per decompressor flop's power-up value
(a form is a bitmask of variables and a constant bit). A gate's output is evaluated
over every value of its inputs' leaves by the simulator's own gate functions (the C++
core's ``eval_gate``) and folded back to a single affine form whenever it is one --
which an XOR that ABC builds from NAND gates is, though no gate on the way is. What a
flop stores, and every scan-in, must fold to an affine form; anything else is
reported. The cycles: one clock with the scan enable low (the preamble every pattern
starts with), then the shifts, the channels taking shift t's word (variables) and 0
in the preamble. A flop's clock must be the chip's clock pin, its clear and preset
held inactive, its enable a constant in every cycle. Shift t's scan-ins must equal
the model's row t: no power-up variable, constant 0.

The decompressor is what the scan-in wires' cones reach, through its flops: a core
cell (``core_prefix``), an input port other than the channels and the scan enable, an
undriven net or a cell FaultFlow has no model of is reported, not followed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from faultflow.control_trace import Netlist, one_net
from faultflow.scan.compression import ContinuousDecompressor
from faultflow.scan.ring_generator import continuous_scan_in_rows

# A cone wider than this many distinct leaves isn't a decompressor's.
_MAX_LEAVES = 10

Form = tuple[int, int]  # (variable bitmask, constant)


@dataclass(frozen=True)
class Value:
    """A Boolean function of ``leaves`` (affine forms): bit i of ``table`` is its
    value when leaf j takes bit j of i."""

    leaves: tuple[Form, ...]
    table: int

    @staticmethod
    def of(form: Form) -> "Value":
        mask, const = form
        if mask == 0:
            return Value((), const & 1)
        return Value(((mask, 0),), 0b10 if const == 0 else 0b01)

    def affine(self) -> Form | None:
        """This value as one affine form, or None when it isn't affine."""
        n = len(self.leaves)
        coefficients = _anf(self.table, n)
        mask = 0
        const = coefficients & 1
        for i in range(n):
            if coefficients >> (1 << i) & 1:
                mask ^= self.leaves[i][0]
                const ^= self.leaves[i][1]
        if coefficients & ~(1 | sum(1 << (1 << i) for i in range(n))):
            return None
        return mask, const


def _anf(table: int, n: int) -> int:
    """The algebraic normal form (Moebius transform) of an n-input truth table:
    bit m is the coefficient of the monomial whose variables are m's set bits."""
    for i in range(n):
        step = 1 << i
        for m in range(1 << n):
            if m & step:
                table ^= ((table >> (m ^ step)) & 1) << m
    return table


@dataclass(frozen=True)
class DecompressorCheckResult:
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


class _Fail(Exception):
    pass


class _Checker:
    def __init__(
        self,
        module: Mapping[str, Any],
        cell_map: Mapping[str, Any],
        decompressor: ContinuousDecompressor,
        core_prefix: str,
    ) -> None:
        from faultflow.runner.runner import _load_core

        core = _load_core()
        if core is None:
            raise _Fail("the decompressor check needs the C++ core (eval_gate)")
        self.core = core
        self.netlist = Netlist(module, cell_map)
        self.decompressor = decompressor
        self.core_prefix = core_prefix
        ports = module.get("ports", {})
        self.port_net: dict[int, tuple[str, int]] = {}
        for name, port in ports.items():
            if not isinstance(port, dict) or port.get("direction") != "input":
                continue
            for index, bit in enumerate(port.get("bits", [])):
                if isinstance(bit, int):
                    self.port_net[bit] = (str(name), index)
        for required in (
            decompressor.channel_port,
            decompressor.scan_enable_port,
            decompressor.clock_port,
        ):
            if required not in ports:
                raise _Fail(f"the chip has no input port {required!r}")
        netnames = module.get("netnames", {})
        self.scan_ins: list[int] = []
        for name in decompressor.scan_in_ports:
            bits = netnames.get(name, {}).get("bits", [])
            if len(bits) != 1 or not isinstance(bits[0], int):
                raise _Fail(f"the chip has no one-bit scan-in wire {name!r}")
            self.scan_ins.append(bits[0])
        self.flops: list[str] = []
        self.gates: dict[int, str] = {}
        self._collect()

    # -- structure ---------------------------------------------------------------

    def _collect(self) -> None:
        """Every cell the scan-ins' cones reach, through the flops: the gates (by
        the net each drives) and the flops; anything outside a decompressor is a
        failure."""
        stack = list(self.scan_ins)
        seen: set[int] = set()
        flops: set[str] = set()
        while stack:
            net = stack.pop()
            if net in seen:
                continue
            seen.add(net)
            if net in self.port_net:
                name = self.port_net[net][0]
                if name == self.decompressor.clock_port:
                    raise _Fail(f"the decompressor reads the clock {name!r} as data")
                if name not in (
                    self.decompressor.channel_port,
                    self.decompressor.scan_enable_port,
                ):
                    raise _Fail(f"the decompressor reads the input port {name!r}")
                continue
            driver = self.netlist.drivers.get(net)
            if driver is None:
                raise _Fail(f"net {net} in the decompressor has no driver")
            instance, _pin = driver
            if instance.startswith(self.core_prefix):
                raise _Fail(f"the decompressor reads the core's {instance}")
            entry = self.netlist.entry(instance)
            if entry is None:
                raise _Fail(f"{instance} in the decompressor isn't in the cell map")
            kind = entry.get("node_type")
            conns = self.netlist.cells[instance].get("connections", {})
            if kind == "CONST" or entry.get("gate_type") in ("CONST0", "CONST1"):
                self.gates[net] = instance
                continue
            if kind == "GATE":
                if len(entry.get("outputs", {})) != 1:
                    raise _Fail(f"{instance} in the decompressor has several outputs")
                self.gates[net] = instance
                stack.extend(
                    bit
                    for pin in entry.get("inputs", [])
                    if isinstance(bit := one_net(conns.get(pin)), int)
                )
                continue
            if kind == "FF":
                if instance not in flops:
                    flops.add(instance)
                    stack.extend(self._flop_inputs(instance, entry, conns))
                continue
            raise _Fail(f"{instance} ({kind}) can't be in a decompressor")
        self.flops = sorted(flops)

    def _flop_inputs(
        self, instance: str, entry: Mapping[str, Any], conns: Mapping[str, Any]
    ) -> list[int]:
        ff = entry.get("ff", {})
        if ff.get("trigger", "POSEDGE") != "POSEDGE":
            raise _Fail(f"decompressor flop {instance} isn't posedge-triggered")
        if "scan" in ff:
            raise _Fail(f"decompressor flop {instance} is a scan flop")
        clock = self.netlist.trace(
            one_net(conns.get(str(ff.get("clock", "CLK")))), instance, "CLK"
        )
        if clock.port != self.decompressor.clock_port or clock.inverted:
            raise _Fail(
                f"decompressor flop {instance} isn't clocked by "
                f"{self.decompressor.clock_port!r}"
            )
        for control in self.netlist.controls(instance):
            if control.trace.const is None or (
                control.trace.value_at_pin(control.trace.const) == control.active
            ):
                raise _Fail(
                    f"decompressor flop {instance}'s {control.kind} isn't held off"
                )
        nets = [one_net(conns.get(str(ff.get("data", "D"))))]
        enable = ff.get("enable")
        if isinstance(enable, dict):
            nets.append(one_net(conns.get(str(enable["pin"]))))
        return [n for n in nets if isinstance(n, int)]

    # -- simulation --------------------------------------------------------------

    def _input(
        self, cell: Mapping[str, Any], pin: str, values: dict[int, Value]
    ) -> Value:
        bit = one_net(cell.get("connections", {}).get(pin))
        if isinstance(bit, str):
            if bit in ("0", "1"):
                return Value((), int(bit))
            raise _Fail(f"an x/z input reaches the decompressor ({bit!r})")
        return self._value(bit, values)

    def _value(self, net: Any, values: dict[int, Value]) -> Value:
        if not isinstance(net, int):
            raise _Fail(f"unexpected net {net!r}")
        if net in values:
            return values[net]
        instance = self.gates.get(net)
        if instance is None:
            raise _Fail(f"net {net} reached during simulation was never collected")
        entry = self.netlist.entry(instance) or {}
        gate = str(entry.get("gate_type", ""))
        if entry.get("node_type") == "CONST" or gate in ("CONST0", "CONST1"):
            value = Value((), int(gate == "CONST1"))
        else:
            cell = self.netlist.cells[instance]
            inputs = [
                self._input(cell, str(p), values) for p in entry.get("inputs", [])
            ]
            value = self._gate(gate, inputs)
        values[net] = value
        return value

    def _gate(self, gate: str, inputs: list[Value]) -> Value:
        leaves: list[Form] = []
        for value in inputs:
            for leaf in value.leaves:
                if leaf not in leaves:
                    leaves.append(leaf)
        if len(leaves) > _MAX_LEAVES:
            raise _Fail(f"a decompressor gate's cone has {len(leaves)} leaves")
        table = 0
        for index in range(1 << len(leaves)):
            bits = []
            for value in inputs:
                position = 0
                for j, leaf in enumerate(value.leaves):
                    position |= ((index >> leaves.index(leaf)) & 1) << j
                bits.append(bool(value.table >> position & 1))
            if self.core.eval_gate(gate, bits):
                table |= 1 << index
        value = Value(tuple(leaves), table)
        folded = value.affine()
        return Value.of(folded) if folded is not None else value

    def run(self, shifts: int) -> list[list[Form]]:
        """The scan-ins at shifts 0..shifts-1 after one preamble clock, as affine
        forms; the flops start from power-up variables."""
        channels = self.decompressor.num_channels
        base = shifts * channels
        state = {
            name: Value.of((1 << (base + i), 0)) for i, name in enumerate(self.flops)
        }
        channel_bits = [
            net
            for net, (name, _index) in sorted(
                self.port_net.items(), key=lambda item: item[1][1]
            )
            if name == self.decompressor.channel_port
        ]
        scan_enable = next(
            net
            for net, (name, _index) in self.port_net.items()
            if name == self.decompressor.scan_enable_port
        )
        rows: list[list[Form]] = []
        for cycle in range(shifts + 1):
            shift = cycle - 1  # cycle 0: the preamble
            values: dict[int, Value] = {scan_enable: Value((), int(shift >= 0))}
            for c, net in enumerate(channel_bits):
                values[net] = (
                    Value.of((1 << (shift * channels + c), 0))
                    if shift >= 0
                    else Value((), 0)
                )
            for name in self.flops:
                entry = self.netlist.entry(name) or {}
                q = one_net(
                    self.netlist.cells[name]
                    .get("connections", {})
                    .get(str(entry.get("ff", {}).get("output", "Q")))
                )
                values[int(q)] = state[name]  # type: ignore[arg-type]
            if shift >= 0:
                row = []
                for net, port in zip(self.scan_ins, self.decompressor.scan_in_ports):
                    form = self._value(net, values).affine()
                    if form is None:
                        raise _Fail(f"{port} at shift {shift} isn't linear")
                    row.append(form)
                rows.append(row)
            state = {name: self._next(name, state[name], values) for name in self.flops}
        return rows

    def _next(self, name: str, current: Value, values: dict[int, Value]) -> Value:
        entry = self.netlist.entry(name) or {}
        ff = entry.get("ff", {})
        cell = self.netlist.cells[name]
        enable = ff.get("enable")
        if isinstance(enable, dict):
            level = self._input(cell, str(enable["pin"]), values).affine()
            if level is None or level[0] != 0:
                raise _Fail(f"decompressor flop {name}'s enable isn't constant")
            active = 1 if str(enable.get("level", "HIGH")).upper() == "HIGH" else 0
            if level[1] != active:
                return current
        data = self._input(cell, str(ff.get("data", "D")), values).affine()
        if data is None:
            raise _Fail(f"what decompressor flop {name} stores isn't linear")
        return Value.of(data)


def check_continuous_decompressor(
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    decompressor: ContinuousDecompressor,
    shifts: int,
    *,
    core_prefix: str = "core_inst__",
) -> DecompressorCheckResult:
    """Check the composed chip's decompressor (``module``: its top module, Yosys
    JSON) against ``decompressor``'s model over ``shifts`` shifts -- a load's
    warm-up and longest chain, and one more for a launch on shift."""
    try:
        checker = _Checker(module, cell_map, decompressor, core_prefix)
        rows = checker.run(shifts)
    except _Fail as failure:
        return DecompressorCheckResult([str(failure)])
    expected = continuous_scan_in_rows(
        decompressor.polynomial,
        decompressor.phase_shifter_taps,
        decompressor.num_channels,
        shifts,
    )
    power_up = shifts * decompressor.num_channels
    errors: list[str] = []
    for chain, port in enumerate(decompressor.scan_in_ports):
        for t in range(shifts):
            mask, const = rows[t][chain]
            if mask >> power_up:
                errors.append(f"{port} at shift {t} depends on a flop's power-up value")
            elif const or mask != expected[t][chain]:
                errors.append(
                    f"{port} at shift {t} isn't the model's: the cells shift in "
                    f"{_describe(mask, const, decompressor.num_channels)}, the model "
                    f"{_describe(expected[t][chain], 0, decompressor.num_channels)}"
                )
            else:
                continue
            break
    return DecompressorCheckResult(errors)


def _describe(mask: int, const: int, channels: int) -> str:
    terms = [
        f"u{i // channels}[{i % channels}]"
        for i in range(mask.bit_length())
        if mask >> i & 1
    ]
    if const:
        terms.append("1")
    return " ^ ".join(terms) or "0"
