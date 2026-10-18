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

from faultflow.pdk.roles import LibraryRoles, default_roles, role_cell
from faultflow.wrap.errors import WrapError

INPUT = "input"
OUTPUT = "output"
SIDES = (INPUT, OUTPUT)

# A boundary cell is three library cells, by role (faultflow.pdk.roles): a
# non-inverting mux2, an and2b and a plain flop.
ROLES = ("mux", "gate", "ff")
# The pins of a boundary cell's mux and gate the wrapper record names (see
# faultflow.wrap.record): Sky130 HD's, which a record leaves out.
HD_RECORD_PINS = {
    "mux_a0": "A0",
    "mux_a1": "A1",
    "mux_s": "S",
    "gate_a_n": "A_N",
    "gate_b": "B",
    "gate_out": "X",
}

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


def add_cell(
    cells: dict[str, Any],
    side: str,
    label: str,
    index: int,
    nets: CellNets,
    roles: LibraryRoles | None = None,
) -> dict[str, str]:
    """Add the boundary cell on port bit ``label`` to a module's ``cells``, of the
    library ``roles`` gives (Sky130 HD by default): its mux, gate and flop instance
    names, by role."""
    roles = roles or default_roles()
    if side not in SIDES:
        raise WrapError(f"a boundary cell's side is {INPUT!r} or {OUTPUT!r}")
    names = instance_names(side, label)
    taken = [name for name in names.values() if name in cells]
    if taken:
        raise WrapError(f"the boundary cell on {label} would reuse {taken[0]}")
    common = {CELL_ATTR: label, SIDE_ATTR: side, INDEX_ATTR: str(index)}
    cells[names["mux"]] = role_cell(
        roles,
        "mux2",
        {**common, ROLE_ATTR: "mux"},
        {"a0": nets.cfi, "a1": nets.q, "s": nets.hold},
        {"out": nets.cfo},
    )
    cells[names["gate"]] = role_cell(
        roles,
        "and2b",
        {**common, ROLE_ATTR: "gate"},
        {"a_n": nets.safe, "b": nets.cfo},
        {"out": nets.out},
    )
    cells[names["ff"]] = role_cell(
        roles,
        "ff",
        {**common, ROLE_ATTR: "ff"},
        {"clk": nets.clock, "d": nets.cfo},
        {"q": nets.q},
    )
    return names
