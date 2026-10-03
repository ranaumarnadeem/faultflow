"""The IJTAG network autoMBIST's ``wrap-test-access`` builds, rebuilt from the
manifest's ``test_access`` block with warptap (an optional dependency), to drive it over
JTAG or to build its integrity program."""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from typing import Any, Protocol

from faultflow.integrations.autombist import AutombistTestAccess


class AutombistJtagError(RuntimeError):
    """warptap isn't importable, or the rebuilt network isn't the one in the netlist."""


class PortInstrument(Protocol):
    """A port behind the IJTAG network: a manifest instrument, or a port
    mbist-insert threads to the top."""

    @property
    def name(self) -> str: ...

    @property
    def role(self) -> str: ...  # "control" | "status"

    @property
    def width(self) -> int: ...

    @property
    def capture_sync(self) -> bool: ...  # a status port captured through 2 TCK flops


def warptap_available() -> bool:
    return importlib.util.find_spec("warptap") is not None


def _require_warptap(what: str) -> None:
    if not warptap_available():
        raise AutombistJtagError(
            f"{what} needs warptap importable (pip install -e <a warptap clone>)"
        )


def instrument_specs(instruments: Sequence[PortInstrument]) -> list[Any]:
    """warptap InstrumentSpecs for ports behind the IJTAG network, in the given
    order: a control port is a WRITE TDR, a status port a READ TDR (capturing
    through a synchronizer when it asks for capture_sync), bound bit for bit to
    the port of its name. The network wrap-test-access builds is made of these."""
    _require_warptap("building IJTAG instruments")
    # warptap is optional: without it, mypy finds no module to check against.
    from warptap.icl_model import (  # type: ignore[import-not-found]
        InstrumentDirection,
        SignalBinding,
    )
    from warptap.sib_plan import InstrumentSpec  # type: ignore[import-not-found]

    directions = {
        "control": InstrumentDirection.WRITE,
        "status": InstrumentDirection.READ,
    }
    specs = []
    for ins in instruments:
        if ins.role not in directions:
            raise AutombistJtagError(
                f"instrument {ins.name!r}: role must be 'control' or 'status', "
                f"got {ins.role!r}"
            )
        if ins.capture_sync and ins.role != "status":
            raise AutombistJtagError(
                f"instrument {ins.name!r}: only a status port captures, so only one "
                "can have capture_sync"
            )
        # Only when asked for: a warptap without capture_sync takes the others.
        sync: dict[str, Any] = {"capture_sync": True} if ins.capture_sync else {}
        specs.append(
            InstrumentSpec(
                ins.name,
                width=ins.width,
                capture_value=0,
                direction=directions[ins.role],
                signal_bits=tuple(SignalBinding(ins.name, b) for b in range(ins.width)),
                **sync,
            )
        )
    return specs


def rebuild_network(access: AutombistTestAccess) -> tuple[Any, Any]:
    """(graph, root) of the network wrap-test-access built: one SIB per instrument, in
    chain order, control ports WRITE and status ports READ, each bound to its port. The
    rebuilt SIB names must be the manifest's, in order."""
    _require_warptap("rebuilding the IJTAG network")
    from warptap.icl_model import slot_name  # type: ignore[import-not-found]
    from warptap.sib_plan import build_sib_plan  # type: ignore[import-not-found]

    specs = instrument_specs(access.instruments)
    graph, root = build_sib_plan(specs, top_name=access.top_module)
    sib_of = {i.hierarchical_path: i.sib_name for i in access.instances}
    expected = [sib_of.get(ins.sib) for ins in access.instruments]
    if [slot_name(node) for node in graph.chain] != expected:
        raise AutombistJtagError(
            "the IJTAG network rebuilt from the manifest is not the one in the "
            f"netlist: SIBs {[slot_name(node) for node in graph.chain]} vs {expected}"
        )
    return graph, root
