"""The JTAG side of mbist-insert: a TAP and an IJTAG network, inserted by
warptap, in place of the test ports threaded to the top.

Every control port becomes a WRITE TDR that also clears on the chip reset
(warptap's chip_reset: test_mode can't come up 1 without TRST), every status
port a READ TDR that captures through two TCK flops (capture_sync: the status
comes from the memory's clock domain), and a dedicated IJTAG_ACCESS instruction
selects the network (ijtag_access_opcode), so a board-level EXTEST can't start a
BIST. The threaded ports are then removed: the chip gains only the TAP's five
pins.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.mbist.netlist import InsertError
from faultflow.mbist.spec import JtagSpec, ResetSpec

TAP_PORTS = ("tck", "tms", "tdi", "tdo", "trst_n")
# The modules warptap adds for this network.
WARPTAP_MODULES = (
    "tap_core",
    "sib_cell",
    "bc1_shift_only",
    "bc1_shift_only_sync",
    "instrument_write",
    "instrument_write_clr",
    "tck_reset_sync",
)
# warptap names every cell and wire it adds to the top with this prefix.
WARPTAP_PREFIX = "warptap_"
# The instance warptap gives the control TDRs' clear synchronizer.
CHIP_RESET_SYNC = "warptap_chip_reset_sync"


@dataclass(frozen=True)
class TestPort:
    """A test port threaded to the top, put behind the network: a control port
    becomes a WRITE TDR, a status port a READ TDR (an autombist_jtag
    PortInstrument)."""

    name: str
    role: str  # "control" | "status"
    width: int
    capture_sync: bool = False


@dataclass(frozen=True)
class JtagInsertion:
    ports: tuple[TestPort, ...]  # in network order, TDI side first
    graph: Any  # warptap PhysicalGraph
    root: Any  # warptap ModuleInstance
    opcode: int  # IJTAG_ACCESS's
    idcode: int
    # Every cell warptap added to the top -> its type.
    cells: dict[str, str]

    def instances(self) -> dict[str, str]:
        """The cells that instantiate warptap's modules -> the module: the TAP,
        the SIBs, the TDR bits and the TDRs' clear synchronizer. (The others
        are the top's own logic: the network's select decode, an inverter.)"""
        return {c: t for c, t in self.cells.items() if not t.startswith("$")}


def check_names(
    modules: dict[str, dict[str, Any]], top: str, taken: Iterable[str]
) -> None:
    """Refuse a design the TAP can't be added to: one with a TAP pin already
    (a chip with its own TAP isn't supported), a module or liberty cell named
    like one warptap adds, or a top-level cell or wire with warptap's prefix."""
    top_module = modules[top]
    pins = [p for p in TAP_PORTS if p in top_module.get("ports", {})]
    if pins:
        raise InsertError(
            f"the top already has port(s) {pins}: mbist-insert adds a JTAG TAP with "
            f"ports {', '.join(TAP_PORTS)}, and a chip with a TAP of its own isn't "
            "supported"
        )
    taken_names = set(taken)
    clash = sorted(m for m in WARPTAP_MODULES if m in modules or m in taken_names)
    if clash:
        raise InsertError(
            f"the design or the liberty already has {clash}, module names the JTAG "
            "TAP and network need"
        )
    named = [*top_module.get("cells", {}), *top_module.get("netnames", {})]
    used = sorted({n for n in named if n.startswith(WARPTAP_PREFIX)})
    if used:
        raise InsertError(
            f"the top already has cells or wires named {used[:5]}: the names "
            f"starting {WARPTAP_PREFIX} are the JTAG network's"
        )


def _require_warptap() -> None:
    from faultflow.integrations.autombist_jtag import warptap_available

    if not warptap_available():
        raise InsertError(
            "jtag needs warptap importable (pip install -e <a warptap clone>)"
        )


def insert_network(
    netlist: dict[str, Any],
    top: str,
    ports: Sequence[TestPort],
    *,
    reset: ResetSpec,
    jtag: JtagSpec,
) -> JtagInsertion:
    """Put a TAP and an IJTAG network into `top` (Yosys JSON, edited in place)
    for `ports`, then remove those ports from the top."""
    _require_warptap()
    # warptap is optional: without it, mypy finds no module to check against.
    from warptap.netlist import Netlist  # type: ignore[import-not-found]
    from warptap.sib_insert import (  # type: ignore[import-not-found]
        SibInsertError,
        insert_sib_network,
    )
    from warptap.sib_plan import build_sib_plan  # type: ignore[import-not-found]
    from warptap.tap_model import (  # type: ignore[import-not-found]
        IDCODE_VALUE,
        OPCODE_IJTAG_ACCESS,
    )

    from faultflow.integrations.autombist_jtag import instrument_specs

    graph, root = build_sib_plan(instrument_specs(ports), top_name=top)
    idcode = jtag.idcode if jtag.idcode is not None else IDCODE_VALUE
    top_module = netlist["modules"][top]
    before = set(top_module.get("cells", {}))
    try:
        insert_sib_network(
            Netlist.from_json(netlist),
            top,
            graph,
            idcode_value=idcode,
            chip_reset=reset.port,
            chip_reset_active_low=reset.active_low,
            ijtag_access_opcode=OPCODE_IJTAG_ACCESS,
        )
    except SibInsertError as exc:
        raise InsertError(f"warptap could not insert the JTAG network: {exc}") from exc
    cells = {
        name: str(cell["type"])
        for name, cell in top_module["cells"].items()
        if name not in before
    }
    _remove_ports(top_module, ports)
    return JtagInsertion(
        ports=tuple(ports),
        graph=graph,
        root=root,
        opcode=OPCODE_IJTAG_ACCESS,
        idcode=idcode,
        cells=cells,
    )


def _remove_ports(top_module: dict[str, Any], ports: Sequence[TestPort]) -> None:
    """Remove the test ports from the top, keeping their nets under their names.
    warptap detached each control port: its old bits, now driven by the TDR,
    are named `<port>_pre_bsr`, and the port's new bits drive nothing."""
    netnames = top_module.setdefault("netnames", {})
    for port in ports:
        del top_module["ports"][port.name]
        if port.role == "control":
            netnames.pop(port.name, None)
            netnames[port.name] = netnames.pop(f"{port.name}_pre_bsr")


def write_descriptions(
    insertion: JtagInsertion, jtag: JtagSpec, top: str, out: Path
) -> tuple[Path, Path]:
    """`<out>/<top>_mbist.icl` and `<top>_mbist.bsd`: the network, its AccessLink
    naming IJTAG_ACCESS, and the TAP-only BSDL that declares it."""
    from warptap.bsdl_emit import (  # type: ignore[import-not-found]
        BsdlEmitError,
        to_bsdl,
    )
    from warptap.icl_emit import to_icl  # type: ignore[import-not-found]

    entity = jtag.bsdl_entity or top
    try:
        bsdl = to_bsdl(
            entity,
            tck_max_freq_hz=jtag.tck_max_mhz * 1e6,
            idcode_value=insertion.idcode,
            ijtag_access_opcode=insertion.opcode,
        )
    except BsdlEmitError as exc:
        raise InsertError(f"cannot write the BSDL: {exc} (jtag.bsdl_entity)") from exc
    icl = to_icl(
        insertion.graph,
        insertion.root,
        include_access_link=True,
        bsdl_entity_name=entity,
        ijtag_access=True,
    )
    icl_path = out / f"{top}_mbist.icl"
    bsdl_path = out / f"{top}_mbist.bsd"
    icl_path.write_text(icl, encoding="utf-8")
    bsdl_path.write_text(bsdl, encoding="utf-8")
    return icl_path, bsdl_path
