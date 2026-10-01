"""The X-isolation proof behind ``ff.py jtag``.

Grading a TCK program with the two-valued simulator is exact only when every value
that can reach TDO is known -- in the fault-free machine and in every faulty one. This
module establishes that structurally, or refuses. Pins are traced with
:mod:`faultflow.control_trace`: through buffers and inverters, and through an AND- or
OR-type gate an input holds at its controlling value.

- A *TAP flop* is clocked from ``tck`` (through buffers and inverters). Each must be
  forced by ``trst_n`` at 0 -- its clear or preset held active by ``trst_n`` at 0
  alone -- so the program's TRST lead-in sets it.
- Every other flop whose clock the holds keep still is *frozen*. One is a known
  constant when its clear or preset is held active by a held input, a constant, the
  chip reset (below), or the output of a flop already known -- the reset synchronizer
  in front of a collar, say -- while nothing can clear or preset it to another value.
  An unknown frozen flop's output is an X source; so is the output of a flop clocked
  from logic, and every blackbox output.
- The X closure -- forward through combinational logic and through any TAP flop an X
  can reach (on any pin) -- must not reach ``tdo``.

Stuck-at faults can't add connectivity, so the closure holds in every faulty machine.
What a fault *can* do is keep a flop out of reset: a stuck-at on a forcing path at
the level that releases it -- up a chain of known flops, too, since a flop that misses
its reset releases those it resets. :attr:`JtagSetup.reset_path_faults` lists those
(site key, fault type) pairs; the grader leaves them ungraded. The same stuck-at at
the forcing level holds the flop in reset, which is deterministic, and is graded.

Holds: every input except ``tck``/``tms``/``tdi``/``trst_n`` is held for the whole
program, at 0 unless a frozen flop's reset needs its input active (e.g. an active-high
reset held at 1); explicit holds override. Clocks held at 0 give no edges.

The chip reset (:class:`ChipReset`) is the exception: when the control TDRs also clear
on it (warptap's chip_reset), holding it active would keep them at 0, so the program
pulses it during its TRST lead-in and then holds it inactive. A frozen flop it resets
keeps its reset value afterwards, its clock held.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.control_trace import (
    Flop,
    Netlist,
    TraceError,
    port_bits,
    release_faults,
)


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
class ChipReset:
    """The chip reset input the program pulses during its TRST lead-in, then holds
    inactive."""

    port: str
    active: int  # the level that resets


@dataclass(frozen=True)
class JtagSetup:
    ports: JtagPorts
    input_order: tuple[str, ...]  # every input port bit, "name" or "name[i]"
    holds: dict[str, int]  # every input bit except tck/tms/tdi/trst_n (and the pulse)
    tap_flops: tuple[str, ...]
    frozen_known: tuple[str, ...]
    x_sources: tuple[int, ...]
    reset_path_faults: frozenset[tuple[str, str]]  # (fault site key, "sa0"/"sa1")
    pulse: ChipReset | None = None


def _known_value(
    net: Netlist,
    flop: Flop,
    pulsed: Mapping[str, int],
    released: Mapping[str, int],
    known: Mapping[int, int],
) -> tuple[int, set[tuple[str, str]]] | None:
    """A frozen flop's value once reset, with the faults that would release it -- or
    None if it isn't known: no control holds it, both do, or the other control could
    clear or preset it after the chip reset's pulse."""
    holding = []
    for control in flop.controls:
        found = net.forced(flop.instance, control.pin, pulsed, known)
        if found is not None and found.value == control.active:
            holding.append((control, found))
    if len(holding) != 1:
        return None
    control, found = holding[0]
    for other in flop.controls:
        if other is control:
            continue
        for values in (pulsed, released):
            held = net.forced(flop.instance, other.pin, values, known)
            if held is None or held.value == other.active:
                return None
    return control.value, release_faults(found)


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
    chip_reset: ChipReset | None = None,
) -> JtagSetup:
    """Classify every flop, settle the holds, and prove TDO X-free -- or raise
    :class:`JtagSetupError` naming what breaks it."""
    data = json.loads(Path(netlist_path).read_text(encoding="utf-8"))
    module = data["modules"][top]
    cell_map = json.loads(Path(cell_map_path).read_text(encoding="utf-8"))
    net = Netlist(module, cell_map)

    inputs = port_bits(module, "input")
    input_names = [name for name, _ in inputs]
    outputs = dict(port_bits(module, "output"))
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
    pulsed_port = None if chip_reset is None else chip_reset.port
    if chip_reset is not None:
        if chip_reset.port not in input_names:
            raise JtagSetupError(
                f"{top} has no single-bit input port {chip_reset.port!r} (the chip "
                "reset the control TDRs clear on)"
            )
        if chip_reset.port in ports.driven():
            raise JtagSetupError(
                f"the chip reset {chip_reset.port} is a TAP port the program drives"
            )
        if chip_reset.port in holds:
            raise JtagSetupError(
                f"the TCK program pulses the chip reset {chip_reset.port} and then "
                "holds it inactive; it can't be held"
            )

    try:
        flops = net.flops()
    except TraceError as exc:
        raise JtagSetupError(str(exc)) from exc
    tap: list[Flop] = []
    others: list[Flop] = []
    for flop in flops:
        if flop.clock.port == ports.tck:
            tap.append(flop)
        elif flop.clock.port is not None and flop.clock.port in ports.driven():
            raise JtagSetupError(
                f"flop {flop.instance}'s clock is neither {ports.tck} nor a held input "
                "(traced through buffers and inverters)"
            )
        else:
            others.append(flop)

    # Default holds: 0, or the level that forces a frozen flop's reset.
    settled: dict[str, int] = {
        name: 0
        for name in input_names
        if name not in ports.driven() and name != pulsed_port
    }
    for flop in others:
        if flop.clock.const is None and flop.clock.port not in settled:
            continue
        for control in flop.controls:
            if control.trace.port is not None and control.trace.port in settled:
                settled[control.trace.port] = control.active ^ int(
                    control.trace.inverted
                )
                break
    settled.update({name: int(value) & 1 for name, value in holds.items()})

    # A clock the holds keep still gives no edges; any other is logic's, and its flop
    # an X source.
    frozen = [
        f for f in others if net.forced(f.instance, f.clock_pin, settled) is not None
    ]
    unclocked = [f for f in others if f not in frozen]

    # trst_n alone: a TAP flop a held input keeps in reset never runs.
    reset_faults: set[tuple[str, str]] = set()
    in_trst = {ports.trst_n: 0}
    for flop in tap:
        forcing = [
            found
            for control in flop.controls
            if (found := net.forced(flop.instance, control.pin, in_trst)) is not None
            and found.value == control.active
        ]
        if not forcing:
            raise JtagSetupError(
                f"TAP flop {flop.instance} isn't forced by {ports.trst_n}=0 (through "
                "buffers, inverters and gates it holds): its state after the TRST "
                "lead-in is unknown"
            )
        for found in forcing:
            reset_faults |= release_faults(found)

    # Known frozen flops, by induction: reset by the holds, the chip reset or a flop
    # already known.
    pulsed = dict(settled)
    released = dict(settled)
    if chip_reset is not None:
        pulsed[chip_reset.port] = chip_reset.active
        released[chip_reset.port] = 1 - chip_reset.active
    known_q: dict[int, int] = {}
    pending = list(frozen)
    while True:
        progress = False
        for flop in list(pending):
            held = _known_value(net, flop, pulsed, released, known_q)
            if held is None:
                continue
            known_q[flop.q], faults = held
            reset_faults |= faults
            pending.remove(flop)
            progress = True
        if not progress:
            break

    x_sources = {flop.q for flop in pending} | {flop.q for flop in unclocked}
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
        frozen_known=tuple(f.instance for f in frozen if f.q in known_q),
        x_sources=tuple(sorted(x_sources)),
        reset_path_faults=frozenset(reset_faults),
        pulse=chip_reset,
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
                f"{sorted(sources)[:8]} (blackbox outputs, flops clocked from logic "
                "and frozen flops nothing keeps in reset) reach it through logic or "
                "TAP flops"
            )
        fresh = {q for q, _pin in reach["flop_inputs"] if q in tap_qs} - x_flops
        if not fresh:
            return
        x_flops |= fresh
        x_nets = x_nets | fresh
