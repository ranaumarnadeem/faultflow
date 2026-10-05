"""One IEEE 1500 wrapper boundary cell, as the sky130 cells that build it.

A boundary cell sits on one port bit: an input cell between an input port bit and the
core logic that read it, an output cell between the core net that drove an output
port bit and the port. It is three sky130 cells:

- a mux (``mux2_1``): A0 the cell's functional input, A1 its flop's Q, select
  ``hold``. Its output is the cell's CFO.
- a gate (``and2b_1``): CFO unless ``safe``. This is the cell's functional output.
- a flop (``dfxtp_1``) capturing CFO. Scan stitching makes it a scan cell, so it
  also shifts.

An input cell's ``hold`` is the INTEST mode net and its ``safe`` the EXTEST one; an
output cell's are the other way round. So in INTEST an input cell drives the core
from its flop and holds that value (it captures its own output), while an output
cell drives its port safe (0) and captures the core's output. EXTEST mirrors it: an
input cell keeps the core at 0 and captures its port, while an output cell drives its
port from its flop and holds it. With both mode nets at 0 the cell passes its
functional input through: the functional mode.

:func:`spec` is that behaviour as a function, and :func:`add_cell` builds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from faultflow.wrap.errors import WrapError

INPUT = "input"
OUTPUT = "output"
SIDES = (INPUT, OUTPUT)

MUX_CELL = "sky130_fd_sc_hd__mux2_1"
GATE_CELL = "sky130_fd_sc_hd__and2b_1"
FLOP_CELL = "sky130_fd_sc_hd__dfxtp_1"
ROLES = ("mux", "gate", "ff")

# Attributes every cell of a boundary cell carries.
CELL_ATTR = "faultflow_wrapper_cell"  # the boundary cell: "port" or "port[bit]"
SIDE_ATTR = "faultflow_wrapper_side"  # input | output
ROLE_ATTR = "faultflow_wrapper_role"  # mux | gate | ff
INDEX_ATTR = "faultflow_wrapper_index"  # its place on the wrapper ring


def spec(
    side: str, intest: int, extest: int, cfi: int, q: int, se: int = 0, sdi: int = 0
) -> tuple[int, int]:
    """A boundary cell's functional output, and the value its flop takes at a clock
    edge: with scan enable ``se`` on (once scanned), the scan input ``sdi``."""
    if side not in SIDES:
        raise ValueError(f"a boundary cell's side is {INPUT!r} or {OUTPUT!r}")
    hold, safe = (intest, extest) if side == INPUT else (extest, intest)
    cfo = q if hold else cfi
    return cfo & (1 - safe), sdi if se else cfo


@dataclass(frozen=True)
class CellNets:
    """A boundary cell's nets. ``cfi`` is a constant ("0", "1", "x") for an output
    cell on a port bit the core ties."""

    cfi: int | str
    out: int
    cfo: int
    q: int
    hold: int
    safe: int
    clock: int


def instance_names(side: str, label: str) -> dict[str, str]:
    """The mux, gate and flop instance names of the boundary cell on port bit
    ``label`` ("port" or "port[bit]")."""
    flat = label.replace("[", "_").replace("]", "")
    stem = f"__wbr_{'i' if side == INPUT else 'o'}_{flat}"
    return {role: f"{stem}_{role}" for role in ROLES}


def _cell(
    kind: str,
    attributes: dict[str, str],
    inputs: dict[str, int | str],
    outputs: dict[str, int],
) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": attributes,
        "port_directions": {
            **{pin: "input" for pin in inputs},
            **{pin: "output" for pin in outputs},
        },
        "connections": {pin: [net] for pin, net in {**inputs, **outputs}.items()},
    }


def add_cell(
    cells: dict[str, Any], side: str, label: str, index: int, nets: CellNets
) -> dict[str, str]:
    """Add the boundary cell on port bit ``label`` to a module's ``cells``: its mux,
    gate and flop instance names, by role."""
    if side not in SIDES:
        raise WrapError(f"a boundary cell's side is {INPUT!r} or {OUTPUT!r}")
    names = instance_names(side, label)
    taken = [name for name in names.values() if name in cells]
    if taken:
        raise WrapError(f"the boundary cell on {label} would reuse {taken[0]}")
    common = {CELL_ATTR: label, SIDE_ATTR: side, INDEX_ATTR: str(index)}
    cells[names["mux"]] = _cell(
        MUX_CELL,
        {**common, ROLE_ATTR: "mux"},
        {"A0": nets.cfi, "A1": nets.q, "S": nets.hold},
        {"X": nets.cfo},
    )
    cells[names["gate"]] = _cell(
        GATE_CELL,
        {**common, ROLE_ATTR: "gate"},
        {"A_N": nets.safe, "B": nets.cfo},
        {"X": nets.out},
    )
    cells[names["ff"]] = _cell(
        FLOP_CELL,
        {**common, ROLE_ATTR: "ff"},
        {"CLK": nets.clock, "D": nets.cfo},
        {"Q": nets.q},
    )
    return names
