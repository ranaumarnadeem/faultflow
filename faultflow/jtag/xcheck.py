"""The X-isolation proof behind ``ff.py jtag``.

Grading a TCK program with the two-valued simulator is exact only when every value
that can reach TDO is known -- in the fault-free machine and in every faulty one. This
module establishes that structurally, or refuses:

- A *TAP flop* is clocked from ``tck`` (through buffers and inverters only). Each must
  be forced by ``trst_n`` at 0 -- its clear or preset traced to ``trst_n`` through
  buffers and inverters, at the right polarity -- so the program's TRST lead-in sets it.
- Every other flop must be *frozen*: clocked from a held input or a constant. It's
  known if a held input or constant keeps its clear or preset active; otherwise its
  output is an X source. Blackbox outputs are X sources too.
- The X closure -- forward through combinational logic and through any TAP flop an X
  can reach (on any pin) -- must not reach ``tdo``.

Stuck-at faults can't add connectivity, so the closure holds in every faulty machine.
What a fault *can* do is keep a flop out of reset: a stuck-at on a traced reset path at
the level that makes the control inactive. :attr:`JtagSetup.reset_path_faults` lists
those (site key, fault type) pairs; the grader leaves them ungraded. The same stuck-at
at the active level holds the flop in reset, which is deterministic, and is graded.

Holds: every input except ``tck``/``tms``/``tdi``/``trst_n`` is held for the whole
program, at 0 unless a frozen flop's reset needs its input active (e.g. an active-high
reset held at 1); explicit holds override. Clocks held at 0 give no edges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key
from faultflow.scan.stitch import _lookup_cell


class JtagSetupError(RuntimeError):
    """The design can't be graded over JTAG as set up; the message says why."""


@dataclass(frozen=True)
class JtagPorts:
    tck: str = "tck"
    tms: str = "tms"
    tdi: str = "tdi"
    trst_n: str = "trst_n"
    tdo: str = "tdo"

    def driven(self) -> tuple[str, ...]:
        return (self.tck, self.tms, self.tdi, self.trst_n)


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
    active: int  # the pin value that forces the flop
    trace: Trace


@dataclass(frozen=True)
class Flop:
    instance: str
    q: int
    clock: Trace
    controls: tuple[Control, ...]


@dataclass(frozen=True)
class JtagSetup:
    ports: JtagPorts
    input_order: tuple[str, ...]  # every input port bit, "name" or "name[i]"
    holds: dict[str, int]  # every input bit except tck/tms/tdi/trst_n
    tap_flops: tuple[str, ...]
    frozen_known: tuple[str, ...]
    x_sources: tuple[int, ...]
    reset_path_faults: frozenset[tuple[str, str]]  # (fault site key, "sa0"/"sa1")


def _one_net(value: Any) -> int | str | None:
    if not isinstance(value, list) or len(value) != 1:
        return None
    bit = value[0]
    return bit if isinstance(bit, (int, str)) else None


def _port_bits(module: Mapping[str, Any], direction: str) -> list[tuple[str, int]]:
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


class _Netlist:
    def __init__(self, module: Mapping[str, Any], cell_map: Mapping[str, Any]) -> None:
        self.cells: dict[str, dict[str, Any]] = dict(module.get("cells", {}))
        self.cell_map = cell_map
        self.input_net_names = {net: name for name, net in _port_bits(module, "input")}
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
            net = _one_net(self.cells[driver].get("connections", {}).get(pin))

    def flops(self) -> list[Flop]:
        flops: list[Flop] = []
        for instance, cell in self.cells.items():
            entry = self.entry(instance)
            if entry is None:
                continue
            if entry.get("node_type") == "LATCH":
                raise JtagSetupError(f"{instance} is a latch; faultflow has no latches")
            if entry.get("node_type") != "FF":
                continue
            ff = entry.get("ff", {})
            conns = cell.get("connections", {})
            q = _one_net(conns.get(ff.get("output", "Q")))
            if not isinstance(q, int):
                raise JtagSetupError(f"flop {instance} has no single-bit output net")
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
                        active=(
                            0 if str(spec.get("level", "LOW")).upper() == "LOW" else 1
                        ),
                        trace=self.trace(
                            _one_net(conns.get(control_pin)), instance, control_pin
                        ),
                    )
                )
            flops.append(
                Flop(
                    instance=instance,
                    q=q,
                    clock=self.trace(
                        _one_net(conns.get(clock_pin)), instance, clock_pin
                    ),
                    controls=tuple(controls),
                )
            )
        return flops


def _held_value(trace: Trace, holds: Mapping[str, int]) -> int | None:
    """The value a traced pin is held at, or None."""
    if trace.const is not None:
        return trace.value_at_pin(trace.const)
    if trace.port is not None and trace.port in holds:
        return trace.value_at_pin(holds[trace.port])
    return None


def _reset_path_faults(control: Control) -> set[tuple[str, str]]:
    """The stuck-ats on ``control``'s traced path that keep the flop out of reset: at
    each net, the value that puts the pin at its inactive level."""
    faults: set[tuple[str, str]] = set()
    for net, consumer, pin, inverted in control.trace.steps:
        stuck = f"sa{(1 - control.active) ^ int(inverted)}"
        faults.add(
            (canonical_site_key(SiteProvenance(yosys_net_id=net, kind="stem")), stuck)
        )
        branch = SiteProvenance(
            yosys_net_id=net, kind="branch", consumer_instance=consumer, input_pin=pin
        )
        faults.add((canonical_site_key(branch), stuck))
    return faults


def analyze(
    core: Any,
    netlist_path: str | Path,
    cell_map_path: str | Path,
    top: str,
    *,
    ports: JtagPorts,
    holds: Mapping[str, int],
    unsupported: str,
    blackbox_instances: Sequence[str] = (),
) -> JtagSetup:
    """Classify every flop, settle the holds, and prove TDO X-free -- or raise
    :class:`JtagSetupError` naming what breaks it."""
    data = json.loads(Path(netlist_path).read_text(encoding="utf-8"))
    module = data["modules"][top]
    cell_map = json.loads(Path(cell_map_path).read_text(encoding="utf-8"))
    net = _Netlist(module, cell_map)

    inputs = _port_bits(module, "input")
    input_names = [name for name, _ in inputs]
    outputs = dict(_port_bits(module, "output"))
    missing = [p for p in ports.driven() if p not in input_names]
    if missing:
        raise JtagSetupError(f"{top} has no single-bit input port {missing[0]!r}")
    if ports.tdo not in outputs:
        raise JtagSetupError(f"{top} has no single-bit output port {ports.tdo!r}")
    unknown = sorted(set(holds) - set(input_names))
    if unknown:
        raise JtagSetupError(f"holds name inputs {top} doesn't have: {unknown}")
    driven_held = sorted(set(holds) & set(ports.driven()))
    if driven_held:
        raise JtagSetupError(
            f"the TCK program drives {driven_held}; they can't be held"
        )

    flops = net.flops()
    tap: list[Flop] = []
    frozen: list[Flop] = []
    for flop in flops:
        if flop.clock.port == ports.tck:
            tap.append(flop)
        elif flop.clock.const is not None or (
            flop.clock.port is not None and flop.clock.port not in ports.driven()
        ):
            frozen.append(flop)
        else:
            raise JtagSetupError(
                f"flop {flop.instance}'s clock is neither {ports.tck} nor a held input "
                "(traced through buffers and inverters)"
            )

    # Default holds: 0, or the level that forces a frozen flop's reset.
    settled: dict[str, int] = {
        name: 0 for name in input_names if name not in ports.driven()
    }
    for flop in frozen:
        for control in flop.controls:
            if control.trace.port is not None and control.trace.port in settled:
                settled[control.trace.port] = control.active ^ int(
                    control.trace.inverted
                )
                break
    settled.update({name: int(value) & 1 for name, value in holds.items()})

    reset_faults: set[tuple[str, str]] = set()
    for flop in tap:
        trst = [
            c
            for c in flop.controls
            if c.trace.port == ports.trst_n and c.trace.value_at_pin(0) == c.active
        ]
        if not trst:
            raise JtagSetupError(
                f"TAP flop {flop.instance} isn't forced by {ports.trst_n}=0 (through "
                "buffers and inverters): its state after the TRST lead-in is unknown"
            )
        for control in trst:
            reset_faults |= _reset_path_faults(control)

    known: list[str] = []
    x_sources: set[int] = set()
    for flop in frozen:
        active = [c for c in flop.controls if _held_value(c.trace, settled) == c.active]
        if active:
            known.append(flop.instance)
            for control in active:
                reset_faults |= _reset_path_faults(control)
        else:
            x_sources.add(flop.q)
    blackboxed = set(blackbox_instances)
    for instance in blackboxed & set(net.cells):
        for pin, bits in net.cells[instance].get("connections", {}).items():
            if net.direction(instance, pin, net.entry(instance)) == "output":
                x_sources.update(b for b in bits if isinstance(b, int))

    _x_closure(
        core,
        str(netlist_path),
        str(cell_map_path),
        unsupported,
        list(blackbox_instances),
        sorted(x_sources),
        {flop.q for flop in tap},
        outputs[ports.tdo],
        ports.tdo,
    )
    return JtagSetup(
        ports=ports,
        input_order=tuple(input_names),
        holds=settled,
        tap_flops=tuple(f.instance for f in tap),
        frozen_known=tuple(known),
        x_sources=tuple(sorted(x_sources)),
        reset_path_faults=frozenset(reset_faults),
    )


def _x_closure(
    core: Any,
    netlist_path: str,
    cell_map_path: str,
    unsupported: str,
    blackbox_instances: list[str],
    sources: list[int],
    tap_qs: set[int],
    tdo_net: int,
    tdo_name: str,
) -> None:
    """Grow the X set through combinational logic and through TAP flops until it stops;
    raise if it reaches TDO."""
    x_nets = set(sources)
    x_flops: set[int] = set()
    while x_nets:
        reach = core.combinational_reach(
            netlist_path, cell_map_path, sorted(x_nets), unsupported, blackbox_instances
        )
        if tdo_net in set(reach["observable"]):
            raise JtagSetupError(
                f"an unknown value can reach {tdo_name}: X sources "
                f"{sorted(sources)[:8]} (blackbox outputs and frozen flops no held "
                "input keeps in reset) reach it through logic or TAP flops"
            )
        fresh = {q for q, _pin in reach["flop_inputs"] if q in tap_qs} - x_flops
        if not fresh:
            return
        x_flops |= fresh
        x_nets = x_nets | fresh
