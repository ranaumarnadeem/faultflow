"""Where a flop's clock, clear and preset come from: the tracer behind ``ff.py jtag``'s
X-isolation proof (:mod:`faultflow.jtag.xcheck`) and the scan test's non-scan cells
(:mod:`faultflow.scan.nonscan`), on a Yosys JSON module of mapped cells.

:meth:`Netlist.trace` follows a net back through buffers and inverters only: to an
input port bit, a constant, or the logic that drives it.

:meth:`Netlist.forced` asks what value a pin is held at, given the values of some
inputs. It follows buffers and inverters, and goes through an AND- or OR-type gate
when an input sits at its controlling value -- 0 into an AND, 1 into an OR, the
opposite on a bubbled input (:data:`CONTROLLING`) -- which sets the gate's output
whatever its other inputs do, or when every input is held. A TDR bit cleared through
``trst_n & clr_n`` is held in reset by ``trst_n`` at 0 that way.

A stuck-at on such a path, at the value that releases the pin, is what can let a flop
out of a reset that holds it; :func:`release_faults` lists them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key
from faultflow.scan.stitch import _lookup_cell

# Gate type -> the value at each input that sets the output, and the output it sets:
# gate_eval.cpp's formulas, with a bubbled input's value flipped.
CONTROLLING: dict[str, tuple[tuple[int, ...], int]] = {
    "AND2": ((0, 0), 0),
    "AND2B": ((0, 1), 0),
    "AND3": ((0, 0, 0), 0),
    "AND3B": ((1, 0, 0), 0),
    "AND4": ((0, 0, 0, 0), 0),
    "AND4B": ((1, 0, 0, 0), 0),
    "AND4BB": ((1, 1, 0, 0), 0),
    "NAND2": ((0, 0), 1),
    "NAND2B": ((1, 0), 1),
    "NAND3": ((0, 0, 0), 1),
    "NAND3B": ((1, 0, 0), 1),
    "NAND4": ((0, 0, 0, 0), 1),
    "NAND4B": ((1, 0, 0, 0), 1),
    "NAND4BB": ((1, 1, 0, 0), 1),
    "OR2": ((1, 1), 1),
    "OR2B": ((1, 0), 1),
    "OR3": ((1, 1, 1), 1),
    "OR3B": ((1, 1, 0), 1),
    "OR4": ((1, 1, 1, 1), 1),
    "OR4B": ((1, 1, 1, 0), 1),
    "OR4BB": ((1, 1, 0, 0), 1),
    "NOR2": ((1, 1), 0),
    "NOR2B": ((1, 0), 0),
    "NOR3": ((1, 1, 1), 0),
    "NOR3B": ((1, 1, 0), 0),
    "NOR4": ((1, 1, 1, 1), 0),
    "NOR4B": ((1, 1, 1, 0), 0),
    "NOR4BB": ((1, 1, 0, 0), 0),
}

# (net, the cell reading it, that cell's pin, the net's value)
Step = tuple[int, str, str, int]


class TraceError(RuntimeError):
    """A netlist the tracer can't follow; the message says why."""


@dataclass(frozen=True)
class Trace:
    """Where a flop pin's net comes from, through buffers and inverters only: an input
    port bit (``port``), a constant, or neither (logic). ``steps`` walks from the pin
    back: (net, the cell reading it, that cell's pin, inverted between this net and the
    pin)."""

    port: str | None
    const: int | None
    inverted: bool
    steps: tuple[tuple[int, str, str, bool], ...]

    def value_at_pin(self, source_value: int) -> int:
        return source_value ^ int(self.inverted)


@dataclass(frozen=True)
class Control:
    kind: str  # "clear" | "preset"
    pin: str
    active: int  # the pin value that forces the flop
    value: int  # the value it forces: the cell map's, 0 if it names none
    trace: Trace


@dataclass(frozen=True)
class Flop:
    instance: str
    q: int
    clock_pin: str
    clock: Trace
    controls: tuple[Control, ...]


@dataclass(frozen=True)
class Forced:
    """A pin held at ``value``. ``steps``: every net on a path that holds it there
    (see :data:`Step`), the pin's own net first."""

    value: int
    steps: tuple[Step, ...]


def one_net(value: Any) -> int | str | None:
    if not isinstance(value, list) or len(value) != 1:
        return None
    bit = value[0]
    return bit if isinstance(bit, (int, str)) else None


def port_bits(module: Mapping[str, Any], direction: str) -> list[tuple[str, int]]:
    """(bit name, net) of every port bit in ``direction``, in netlist order."""
    bits: list[tuple[str, int]] = []
    for name, port in module.get("ports", {}).items():
        if not isinstance(port, dict) or port.get("direction") != direction:
            continue
        single = len(port.get("bits", [])) == 1
        for index, net in enumerate(port.get("bits", [])):
            if isinstance(net, int):
                bits.append((name if single else f"{name}[{index}]", net))
    return bits


def release_faults(forced: Forced) -> set[tuple[str, str]]:
    """The stuck-ats on ``forced``'s paths that release the pin: at each net, the
    value opposite the one it holds -- as (fault site key, "sa0"/"sa1"), on the stem
    and on the branch into the cell reading it."""
    faults: set[tuple[str, str]] = set()
    for net, consumer, pin, value in forced.steps:
        stuck = f"sa{1 - value}"
        faults.add(
            (canonical_site_key(SiteProvenance(yosys_net_id=net, kind="stem")), stuck)
        )
        branch = SiteProvenance(
            yosys_net_id=net, kind="branch", consumer_instance=consumer, input_pin=pin
        )
        faults.add((canonical_site_key(branch), stuck))
    return faults


class Netlist:
    def __init__(self, module: Mapping[str, Any], cell_map: Mapping[str, Any]) -> None:
        self.cells: dict[str, dict[str, Any]] = dict(module.get("cells", {}))
        self.cell_map = cell_map
        self.input_net_names = {net: name for name, net in port_bits(module, "input")}
        self.drivers: dict[int, tuple[str, str]] = {}
        for instance, cell in self.cells.items():
            entry = self.entry(instance)
            for pin, bits in cell.get("connections", {}).items():
                if self.direction(instance, pin, entry) != "output":
                    continue
                for bit in bits:
                    if isinstance(bit, int):
                        self.drivers[bit] = (instance, pin)

    def entry(self, instance: str) -> dict[str, Any] | None:
        found = _lookup_cell(
            dict(self.cell_map), str(self.cells[instance].get("type", ""))
        )
        return found[1] if found is not None else None

    def direction(self, instance: str, pin: str, entry: dict[str, Any] | None) -> str:
        directions = self.cells[instance].get("port_directions", {})
        if isinstance(directions, dict) and pin in directions:
            return str(directions[pin])
        if entry is not None and pin in entry.get("outputs", {}):
            return "output"
        return "input"

    def trace(self, net: Any, consumer: str, pin: str) -> Trace:
        steps: list[tuple[int, str, str, bool]] = []
        inverted = False
        seen: set[int] = set()
        while True:
            if isinstance(net, str):
                if net in ("0", "1"):
                    return Trace(None, int(net), inverted, tuple(steps))
                return Trace(None, None, inverted, tuple(steps))  # x/z
            steps.append((net, consumer, pin, inverted))
            if net in self.input_net_names:
                return Trace(self.input_net_names[net], None, inverted, tuple(steps))
            if net in seen or net not in self.drivers:
                return Trace(None, None, inverted, tuple(steps))
            seen.add(net)
            driver, _out = self.drivers[net]
            entry = self.entry(driver) or {}
            gate = entry.get("gate_type")
            if entry.get("node_type") == "CONST" or gate in ("CONST0", "CONST1"):
                return Trace(None, int(gate == "CONST1"), inverted, tuple(steps))
            if entry.get("node_type") != "GATE" or gate not in ("BUF", "INV"):
                return Trace(None, None, inverted, tuple(steps))
            inputs = entry.get("inputs", [])
            if len(inputs) != 1:
                return Trace(None, None, inverted, tuple(steps))
            inverted ^= gate == "INV"
            consumer, pin = driver, str(inputs[0])
            net = one_net(self.cells[driver].get("connections", {}).get(pin))

    def forced(
        self,
        instance: str,
        pin: str,
        values: Mapping[str, int],
        known: Mapping[int, int] | None = None,
    ) -> Forced | None:
        """The value ``instance``'s input ``pin`` is held at, or None. Its net is
        followed back through buffers, inverters and AND/OR-type gates
        (:data:`CONTROLLING`) one input sets or every input holds, to constants, to
        input port bits ``values`` holds (by bit name) and to nets ``known`` holds
        (outputs of flops known to sit still)."""
        net = one_net(self.cells[instance].get("connections", {}).get(pin))
        held = self._held(net, values, known or {}, {}, set())
        if held is None:
            return None
        value, upstream = held
        own: tuple[Step, ...] = (
            ((net, instance, pin, value),) if isinstance(net, int) else ()
        )
        return Forced(value, own + upstream)

    def _held(
        self,
        net: Any,
        values: Mapping[str, int],
        known: Mapping[int, int],
        memo: dict[int, tuple[int, tuple[Step, ...]] | None],
        visiting: set[int],
    ) -> tuple[int, tuple[Step, ...]] | None:
        """``net``'s held value and the steps upstream of it, or None."""
        if isinstance(net, str):
            return (int(net), ()) if net in ("0", "1") else None  # x/z: unknown
        if not isinstance(net, int) or net in visiting:
            return None
        if net not in memo:
            visiting.add(net)
            memo[net] = self._resolve(net, values, known, memo, visiting)
            visiting.discard(net)
        return memo[net]

    def _resolve(
        self,
        net: int,
        values: Mapping[str, int],
        known: Mapping[int, int],
        memo: dict[int, tuple[int, tuple[Step, ...]] | None],
        visiting: set[int],
    ) -> tuple[int, tuple[Step, ...]] | None:
        if net in self.input_net_names:
            name = self.input_net_names[net]
            return (int(values[name]) & 1, ()) if name in values else None
        if net in known:
            return int(known[net]) & 1, ()
        if net not in self.drivers:
            return None
        driver = self.drivers[net][0]
        entry = self.entry(driver) or {}
        gate = str(entry.get("gate_type", ""))
        if entry.get("node_type") == "CONST" or gate in ("CONST0", "CONST1"):
            return int(gate == "CONST1"), ()
        if entry.get("node_type") != "GATE":
            return None
        inputs = [str(p) for p in entry.get("inputs", [])]
        conns = self.cells[driver].get("connections", {})
        if gate in ("BUF", "INV") and len(inputs) == 1:
            source = one_net(conns.get(inputs[0]))
            held = self._held(source, values, known, memo, visiting)
            if held is None:
                return None
            value, upstream = held
            own = (
                ((source, driver, inputs[0], value),) if isinstance(source, int) else ()
            )
            return value ^ int(gate == "INV"), own + upstream
        rule = CONTROLLING.get(gate)
        if rule is None or len(rule[0]) != len(inputs):
            return None
        setting, output = rule
        setters: list[Step] = []
        every: list[Step] = []
        set_by_one = False
        all_held = True
        for input_pin, controlling in zip(inputs, setting):
            source = one_net(conns.get(input_pin))
            held = self._held(source, values, known, memo, visiting)
            if held is None:
                all_held = False
                continue
            path: list[Step] = list(held[1])
            if isinstance(source, int):
                path.insert(0, (source, driver, input_pin, held[0]))
            every.extend(path)
            if held[0] == controlling:
                set_by_one = True
                setters.extend(path)
        if set_by_one:
            return output, tuple(setters)
        # Every input held, none at its controlling value: the other output, which
        # any one of them flipped would change.
        return (1 - output, tuple(every)) if all_held else None

    def flops(self) -> list[Flop]:
        flops: list[Flop] = []
        for instance, cell in self.cells.items():
            entry = self.entry(instance)
            if entry is None:
                continue
            if entry.get("node_type") == "LATCH":
                raise TraceError(f"{instance} is a latch; faultflow has no latches")
            if entry.get("node_type") != "FF":
                continue
            ff = entry.get("ff", {})
            conns = cell.get("connections", {})
            q = one_net(conns.get(ff.get("output", "Q")))
            if not isinstance(q, int):
                raise TraceError(f"flop {instance} has no single-bit output net")
            clock_pin = str(ff.get("clock", "CLK"))
            controls = []
            for kind in ("clear", "preset"):
                spec = ff.get(kind)
                if not isinstance(spec, dict):
                    continue
                control_pin = str(spec["pin"])
                controls.append(
                    Control(
                        kind=kind,
                        pin=control_pin,
                        active=(
                            0 if str(spec.get("level", "LOW")).upper() == "LOW" else 1
                        ),
                        value=int(spec.get("value", 0)),
                        trace=self.trace(
                            one_net(conns.get(control_pin)), instance, control_pin
                        ),
                    )
                )
            flops.append(
                Flop(
                    instance=instance,
                    q=q,
                    clock_pin=clock_pin,
                    clock=self.trace(
                        one_net(conns.get(clock_pin)), instance, clock_pin
                    ),
                    controls=tuple(controls),
                )
            )
        return flops
