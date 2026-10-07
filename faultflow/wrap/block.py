"""Wrap a module's ports in IEEE 1500 boundary cells (:mod:`faultflow.wrap.cell`).

:func:`wrap_block` adds the two mode pins, INTEST and EXTEST, and a boundary cell on
every port bit :func:`~faultflow.wrap.classify.classify` wraps, in port order --
inputs first, then outputs -- which is their place on the wrapper ring:
- an input cell reads the port bit, and every cell that read the port bit reads the
  cell's output instead (so does an output port that echoed the input);
- an output cell reads what drove the port bit and drives the port bit itself. A
  bit the core leaves unknown (x) reads 0, as FaultFlow simulates it.

The cells' flops run on the wrapper clock: the design's one clock, or the port
``clock`` names (a block without flops gets it as a new input). The module records
the wrapper in its attributes -- control, mode pins and clock -- for scan insertion.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from faultflow.wrap.cell import INPUT, OUTPUT, CellNets, add_cell
from faultflow.wrap.classify import Decision, classify, clock_ports
from faultflow.wrap.errors import WrapError

CONTROL_ATTR = "faultflow_wrapper_control"
INTEST_ATTR = "faultflow_wrapper_intest"
EXTEST_ATTR = "faultflow_wrapper_extest"
CLOCK_ATTR = "faultflow_wrapper_clock"


@dataclass(frozen=True)
class WrapOptions:
    """The wrapper clock port ("": the design's one clock), the mode pins, and the
    port or "port[bit]" globs left unwrapped."""

    clock: str = ""
    intest_pin: str = "wbr_intest"
    extest_pin: str = "wbr_extest"
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class WrappedCell:
    label: str
    side: str
    index: int
    instances: dict[str, str]
    nets: CellNets


@dataclass(frozen=True)
class WrapResult:
    netlist: dict[str, Any]
    top: str
    decisions: list[Decision]
    cells: list[WrappedCell]
    clock: str


def _next_id(module: Mapping[str, Any]) -> int:
    used: set[int] = set()
    for port in module.get("ports", {}).values():
        used.update(b for b in port.get("bits", []) if isinstance(b, int))
    for cell in module.get("cells", {}).values():
        for bits in cell.get("connections", {}).values():
            used.update(b for b in bits if isinstance(b, int))
    for name in module.get("netnames", {}).values():
        used.update(b for b in name.get("bits", []) if isinstance(b, int))
    return max(used) + 1 if used else 2


def _flat(label: str) -> str:
    return label.replace("[", "_").replace("]", "")


def _clock(
    ports: dict[str, Any], decisions: list[Decision], named: str
) -> tuple[str, bool]:
    """The wrapper clock port, and whether it has to be added."""
    clocks = clock_ports(decisions)
    if not named:
        if len(clocks) == 1:
            return clocks[0], False
        if not clocks:
            raise WrapError(
                "the block has no clock: name the wrapper clock port to add "
                "([wrap] clock)"
            )
        raise WrapError(
            f"the block has clocks {', '.join(clocks)}: name the wrapper clock "
            "([wrap] clock)"
        )
    if named in clocks:
        return named, False
    if named in ports:
        reason = next(
            (d.reason for d in decisions if d.bit.label == named), "not an input bit"
        )
        raise WrapError(
            f"the wrapper clock {named} is a port that isn't a clock: {reason}"
        )
    if clocks:
        raise WrapError(
            f"the wrapper clock {named} isn't a port; the block's clocks are "
            f"{', '.join(clocks)}"
        )
    return named, True


def wrap_block(
    netlist: Mapping[str, Any],
    top: str,
    cell_map: Mapping[str, Any],
    options: WrapOptions = WrapOptions(),
) -> WrapResult:
    """``netlist`` with its ``top`` module wrapped: a copy, the input left as it is."""
    wrapped = copy.deepcopy(dict(netlist))
    modules = wrapped.get("modules")
    if not isinstance(modules, dict) or top not in modules:
        raise WrapError(f"top module {top!r} not found in the netlist")
    module = modules[top]
    ports: dict[str, Any] = module.setdefault("ports", {})
    cells: dict[str, Any] = module.setdefault("cells", {})
    netnames: dict[str, Any] = module.setdefault("netnames", {})
    decisions = classify(module, cell_map, options.exclude)
    if options.intest_pin == options.extest_pin:
        raise WrapError("the INTEST and EXTEST mode pins need two names")
    for pin in (options.intest_pin, options.extest_pin):
        if pin in ports:
            raise WrapError(f"the mode pin {pin} is already a port")
    clock, add_clock = _clock(ports, decisions, options.clock)
    next_id = _next_id(module)

    def name_net(name: str, bits: list[int | str]) -> None:
        if name in netnames:
            raise WrapError(f"the wrapper's net name {name} is already taken")
        netnames[name] = {"hide_name": 0, "bits": bits, "attributes": {}}

    def net(name: str) -> int:
        nonlocal next_id
        bit = next_id
        next_id += 1
        name_net(name, [bit])
        return bit

    def add_input(name: str) -> int:
        bit = net(name)
        ports[name] = {"direction": "input", "bits": [bit]}
        return bit

    intest = add_input(options.intest_pin)
    extest = add_input(options.extest_pin)
    if add_clock:
        clock_net = add_input(clock)
    else:
        found = next(d.bit.net for d in decisions if d.bit.label == clock)
        if not isinstance(found, int):
            raise WrapError(f"the wrapper clock {clock} has no net")
        clock_net = found
    original = list(cells)
    outputs = [p for p in ports.values() if p.get("direction") == "output"]
    wrapped_cells: list[WrappedCell] = []

    def add(side: str, label: str, cfi: int | str, out: int) -> None:
        flat = _flat(label)
        hold, safe = (intest, extest) if side == INPUT else (extest, intest)
        nets = CellNets(
            cfi=cfi,
            out=out,
            cfo=net(f"__wbr_cfo_{flat}"),
            q=net(f"__wbr_q_{flat}"),
            hold=hold,
            safe=safe,
            clock=clock_net,
        )
        index = len(wrapped_cells)
        instances = add_cell(cells, side, label, index, nets)
        wrapped_cells.append(WrappedCell(label, side, index, instances, nets))

    chosen = [d.bit for d in decisions if d.wrapped]
    for bit in (b for b in chosen if b.direction == "input"):
        if not isinstance(bit.net, int):
            raise WrapError(f"input {bit.label} has no net")
        core = net(f"__wbr_core_{_flat(bit.label)}")
        for instance in original:
            connections = cells[instance].get("connections", {})
            for pin, bits in connections.items():
                connections[pin] = [core if b == bit.net else b for b in bits]
        for port in outputs:
            port["bits"] = [core if b == bit.net else b for b in port["bits"]]
        add(INPUT, bit.label, bit.net, core)
    core_names: dict[str, list[int | str]] = {}
    for bit in (b for b in chosen if b.direction == "output"):
        port = ports[bit.port]
        core_names.setdefault(bit.port, list(port["bits"]))
        driver = port["bits"][bit.index]
        cfi = driver if isinstance(driver, int) or driver in ("0", "1") else "0"
        sys = net(f"__wbr_sys_{_flat(bit.label)}")
        port["bits"][bit.index] = sys
        add(OUTPUT, bit.label, cfi, sys)
    for name, bits in core_names.items():
        name_net(f"__wbr_core_{name}", bits)
        netnames.pop(name, None)
        name_net(name, list(ports[name]["bits"]))
    if not wrapped_cells:
        raise WrapError("no port bit to wrap")
    module.setdefault("attributes", {}).update(
        {
            CONTROL_ATTR: "pins",
            INTEST_ATTR: options.intest_pin,
            EXTEST_ATTR: options.extest_pin,
            CLOCK_ATTR: clock,
        }
    )
    return WrapResult(wrapped, top, decisions, wrapped_cells, clock)


def format_decisions(result: WrapResult) -> str:
    """A table of every port bit: whether it got a boundary cell, and why."""
    rows = [("port bit", "boundary cell", "reason")]
    for decision in result.decisions:
        rows.append(
            (
                decision.bit.label,
                "yes" if decision.wrapped else "no",
                decision.reason,
            )
        )
    widths = [max(len(row[k]) for row in rows) for k in range(2)]
    return "\n".join(
        f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]}" for row in rows
    )
