"""The IJTAG network autoMBIST's ``wrap-test-access`` builds, rebuilt from the
manifest's ``test_access`` block with warptap (an optional dependency), to drive it over
JTAG or to build its integrity program."""

from __future__ import annotations

import importlib.util
from typing import Any

from faultflow.integrations.autombist import AutombistTestAccess


class AutombistJtagError(RuntimeError):
    """warptap isn't importable, or the rebuilt network isn't the one in the netlist."""


def warptap_available() -> bool:
    return importlib.util.find_spec("warptap") is not None


def rebuild_network(access: AutombistTestAccess) -> tuple[Any, Any]:
    """(graph, root) of the network wrap-test-access built: one SIB per instrument, in
    chain order, control ports WRITE and status ports READ, each bound to its port. The
    rebuilt SIB names must be the manifest's, in order."""
    if not warptap_available():
        raise AutombistJtagError(
            "rebuilding the IJTAG network needs warptap importable "
            "(e.g. PYTHONPATH=~/warptap/src)"
        )
    # warptap is optional: without it, mypy finds no module to check against.
    from warptap.icl_model import (  # type: ignore[import-not-found]
        InstrumentDirection,
        SignalBinding,
        slot_name,
    )
    from warptap.sib_plan import (  # type: ignore[import-not-found]
        InstrumentSpec,
        build_sib_plan,
    )

    specs = [
        InstrumentSpec(
            ins.name,
            width=ins.width,
            capture_value=0,
            direction=(
                InstrumentDirection.WRITE
                if ins.role == "control"
                else InstrumentDirection.READ
            ),
            signal_bits=tuple(SignalBinding(ins.name, b) for b in range(ins.width)),
        )
        for ins in access.instruments
    ]
    graph, root = build_sib_plan(specs, top_name=access.top_module)
    sib_of = {i.hierarchical_path: i.sib_name for i in access.instances}
    expected = [sib_of.get(ins.sib) for ins in access.instruments]
    if [slot_name(node) for node in graph.chain] != expected:
        raise AutombistJtagError(
            "the IJTAG network rebuilt from the manifest is not the one in the "
            f"netlist: SIBs {[slot_name(node) for node in graph.chain]} vs {expected}"
        )
    return graph, root
