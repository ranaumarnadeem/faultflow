"""Which test owns each fault of a wrapped block: the block's own INTEST, or the
EXTEST of the SoC the block sits in.

A fault belongs to the mode that can activate it:
- The system side of each boundary cell belongs to EXTEST: an input cell's port bit
  (and its branch into the cell's mux), and an output cell's mux input from its
  flop, its gate's input from the mux and its port bit. Only EXTEST drives an input
  port or observes an output port.
- A stuck-at on a mode net belongs to the mode that holds the net at the other value.
  INTEST holds INTEST at 1 and EXTEST at 0, EXTEST the reverse, so INTEST stuck at 0
  and EXTEST stuck at 1 are INTEST's, and the other two EXTEST's.
- Everything else -- the core, and the rest of each cell -- belongs to INTEST.

On a SoC (its record, faultflow.project.soc_wrapper) the glue between its blocks is
EXTEST's too: a branch into a glue cell, the stem of a net a glue cell drives, and
the stem of a SoC input EXTEST must drive -- one that fans out, or reaches glue. A
SoC input read by one block's core alone is that block's input, and INTEST's. The
SoC's mode nets -- its own pins, and its glue's on their way to its blocks -- are
EXTEST's whichever way they're stuck: no INTEST runs on the SoC, so a stuck-at only
INTEST would activate is counted there and never credited. A branch into a block's
cell is the block's, by the rule above.

Each mode's campaign leaves the other mode's faults to it, as ``wbr_decoupled``
(:func:`decoupled`), so every fault is graded where its test runs, once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from faultflow.wrap.cell import INPUT
from faultflow.wrap.graybox import CORE_TYPE
from faultflow.wrap.record import mode_nets

INTEST = "intest"
EXTEST = "extest"
MODES = (INTEST, EXTEST)
FAULT_TYPES = ("sa0", "sa1")


@dataclass(frozen=True)
class Sides:
    """What decides a fault's owner: the mode nets (each with the value INTEST
    holds it at), the system-side nets and (net or None for any, consumer, pin)
    branches of the boundary cells, and on a SoC its glue cells and the stems
    EXTEST owns for the glue."""

    held: Mapping[int, int]
    nets: frozenset[int]
    branches: frozenset[tuple[int | None, str, str]]
    glue_cells: frozenset[str] = frozenset()
    glue_stems: frozenset[int] = frozenset()


def glue_cells(module: Mapping[str, Any]) -> frozenset[str]:
    """A SoC's glue cells: none of its blocks', which the composition tagged with
    their block (``faultflow_block``), nor a graybox's stub of a core."""
    cells = module.get("cells", {})
    if not any(
        cell.get("attributes", {}).get("faultflow_block") for cell in cells.values()
    ):
        raise ValueError("the SoC's cells name no block (faultflow_block)")
    return frozenset(
        str(name)
        for name, cell in cells.items()
        if not cell.get("attributes", {}).get("faultflow_block")
        and cell.get("type") != CORE_TYPE
    )


def _glue(
    module: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]
) -> tuple[frozenset[str], frozenset[int]]:
    """A SoC's glue cells, and the stems EXTEST owns for them: what a glue cell
    drives, and each SoC input that fans out or reaches a glue cell."""
    cells = module.get("cells", {})
    glue = glue_cells(module)
    stems: set[int] = set()
    readers: dict[int, set[str]] = {}
    for name, cell in cells.items():
        directions = cell.get("port_directions", {})
        for pin, bits in cell.get("connections", {}).items():
            nets = {bit for bit in bits if isinstance(bit, int)}
            if directions.get(pin) == "output":
                if name in glue:
                    stems |= nets
            else:
                for net in nets:
                    readers.setdefault(net, set()).add(str(name))
    fanned = {int(row["yosys_net_id"]) for row in rows if row["kind"] == "branch"}
    for port in module.get("ports", {}).values():
        if port.get("direction") != "input":
            continue
        for net in port.get("bits", []):
            if not isinstance(net, int):
                continue
            if net in fanned or readers.get(net, set()) & glue:
                stems.add(net)
    return glue, frozenset(stems)


def sides_of(
    wrapper: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]] = (),
    module: Mapping[str, Any] | None = None,
) -> Sides:
    """The Sides of `wrapper`'s faults; a SoC's needs its netlist `module` and that
    netlist's fault site `rows`."""
    nets: set[int] = set()
    branches: set[tuple[int | None, str, str]] = set()
    for cell in wrapper["cells"]:
        sys_net = cell["sys_net"]
        if isinstance(sys_net, int):
            nets.add(sys_net)
        if cell["side"] == INPUT:
            if isinstance(sys_net, int):
                branches.add((sys_net, str(cell["mux"]), "A0"))
        else:
            branches.add((None, str(cell["mux"]), "A1"))
            branches.add((None, str(cell["gate"]), "B"))
    glue_cells: frozenset[str] = frozenset()
    glue_stems: frozenset[int] = frozenset()
    if "blocks" in wrapper:
        if module is None:
            raise ValueError("a SoC's fault owners need its netlist")
        glue_cells, glue_stems = _glue(module, rows)
    return Sides(
        mode_nets(wrapper),
        frozenset(nets),
        frozenset(branches),
        glue_cells,
        glue_stems,
    )


def owner(
    row: Mapping[str, Any],
    fault_type: str,
    wrapper: Mapping[str, Any],
    *,
    _sides: Sides | None = None,
) -> str:
    """INTEST or EXTEST: the mode that owns ``fault_type`` on the fault site ``row``
    (a row of the C++ core's list_site_keys) of a block. A SoC's need its Sides
    (sides_of)."""
    sides = _sides if _sides is not None else sides_of(wrapper)
    net = int(row["yosys_net_id"])
    consumer = str(row.get("consumer_instance", ""))
    if net in sides.held:
        # A SoC's mode nets are its own pins and glue: its EXTEST owns their stems
        # and what its glue reads of them, whichever way they're stuck.
        on_soc = "blocks" in wrapper and (
            str(row["kind"]) == "stem" or consumer in sides.glue_cells
        )
        stuck = int(fault_type.lower().removeprefix("sa"))
        return INTEST if stuck != sides.held[net] and not on_soc else EXTEST
    if str(row["kind"]) == "stem":
        return EXTEST if net in sides.nets or net in sides.glue_stems else INTEST
    pin = str(row.get("input_pin", ""))
    if consumer in sides.glue_cells:
        return EXTEST
    if (net, consumer, pin) in sides.branches or (
        None,
        consumer,
        pin,
    ) in sides.branches:
        return EXTEST
    return INTEST


def decoupled(
    rows: Iterable[Mapping[str, Any]],
    wrapper: Mapping[str, Any],
    mode: str,
    *,
    module: Mapping[str, Any] | None = None,
) -> frozenset[tuple[str, str]]:
    """The (site key, fault type) of every fault on ``rows`` the other mode owns: what
    ``mode``'s campaign leaves to it. A SoC's need its netlist ``module``."""
    if mode not in MODES:
        raise ValueError(f"no wrapper test mode {mode!r}")
    rows = list(rows)
    sides = sides_of(wrapper, rows, module)
    return frozenset(
        (str(row["site_key"]), fault_type)
        for row in rows
        for fault_type in FAULT_TYPES
        if owner(row, fault_type, wrapper, _sides=sides) != mode
    )
