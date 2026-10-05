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

Each mode's campaign leaves the other mode's faults to it, as ``wbr_decoupled``
(:func:`decoupled`), so every fault is graded where its test runs, once.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from faultflow.wrap.cell import INPUT

INTEST = "intest"
EXTEST = "extest"
MODES = (INTEST, EXTEST)
FAULT_TYPES = ("sa0", "sa1")


def _system_side(
    wrapper: Mapping[str, Any],
) -> tuple[set[int], set[tuple[int | None, str, str]]]:
    """The system-side nets, and the (net or None for any, consumer, pin) branches,
    of the wrapper's cells."""
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
    return nets, branches


def owner(
    row: Mapping[str, Any],
    fault_type: str,
    wrapper: Mapping[str, Any],
    *,
    _sides: tuple[set[int], set[tuple[int | None, str, str]]] | None = None,
) -> str:
    """INTEST or EXTEST: the mode that owns ``fault_type`` on the fault site ``row``
    (a row of the C++ core's list_site_keys)."""
    net = int(row["yosys_net_id"])
    held = {int(wrapper["intest"]["net"]): 1, int(wrapper["extest"]["net"]): 0}
    if net in held:
        stuck = int(fault_type.lower().removeprefix("sa"))
        return INTEST if stuck != held[net] else EXTEST
    nets, branches = _sides if _sides is not None else _system_side(wrapper)
    if str(row["kind"]) == "stem":
        return EXTEST if net in nets else INTEST
    consumer = str(row.get("consumer_instance", ""))
    pin = str(row.get("input_pin", ""))
    if (net, consumer, pin) in branches or (None, consumer, pin) in branches:
        return EXTEST
    return INTEST


def decoupled(
    rows: Iterable[Mapping[str, Any]], wrapper: Mapping[str, Any], mode: str
) -> frozenset[tuple[str, str]]:
    """The (site key, fault type) of every fault on ``rows`` the other mode owns: what
    ``mode``'s campaign leaves to it."""
    if mode not in MODES:
        raise ValueError(f"no wrapper test mode {mode!r}")
    sides = _system_side(wrapper)
    return frozenset(
        (str(row["site_key"]), fault_type)
        for row in rows
        for fault_type in FAULT_TYPES
        if owner(row, fault_type, wrapper, _sides=sides) != mode
    )
