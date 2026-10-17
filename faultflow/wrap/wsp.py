"""The IEEE 1500 wrapper serial port (WSP), its instruction register (WIR) and bypass
register (WBY): ``[wrap] control = wsp``, the wrapper's mode set by an instruction
shifted in, in place of the two mode pins. :mod:`faultflow.wrap.wsp_model` is its
behaviour, the reference the netlist is checked against.

:func:`add_wsp` (with the boundary cells, before scan) adds the WSP's ports -- WRCK,
WRSTN, SelectWIR, ShiftWR, CaptureWR, UpdateWR, WSI in; WSO out -- and as sky130 cells:

- the WIR: a 3-bit shift stage (``dfxtp_1`` on WRCK, WSI into bit 0) that shifts while
  SelectWIR and ShiftWR are 1, else keeps its value through a mux; and an update stage
  (``dfrtn_1``: the falling edge of WRCK, cleared by WRSTN to WS_BYPASS) that takes the
  shift stage while SelectWIR and UpdateWR are 1, else keeps its value.
- the WBY: a ``dfrtp_1`` on WRCK cleared by WRSTN, shifting WSI while ShiftWR is 1 and
  capturing 0 while CaptureWR is 1, the WIR not selected.
- the decode, onto the four nets the boundary cells read (an input cell's hold and
  safe, an output cell's): in a parallel instruction INTEST's or EXTEST's, as the mode
  pins set them; in a serial one every cell also holds unless the WBR captures
  (CaptureWR, the WIR deselected).
- WSO: the WIR's bit 2 while SelectWIR is 1, else WBY.

The boundary cells' flops run on WRCK. The WIR, WBY and decode cells carry
:data:`WSP_ATTR`; scan insertion leaves the WSP's flops out of the chains (reason
``wsp``). Once the wrapper's flops are on their chains, :func:`link_wsp` gives the
serial instructions their path: each wrapper chain's head takes the chain before's
last flop (the first takes WSI) when the instruction is serial, else its own scan
input; the wrapper flops shift with ShiftWR in a serial instruction, beside the scan
enable; and WSO, in a serial instruction with the WIR deselected, is the last wrapper
chain's last flop. In a parallel instruction (or WS_BYPASS) those paths are the
plain scan paths, which scan-check follows through them.

A scan test runs with the WIR programmed and held (:mod:`faultflow.wrap.wsp_setup`),
so it can't test the WSP: it leaves the WSP's fault sites (:func:`wsp_sites`) to
the WSP's own test (exclusion ``wsp``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from faultflow.control_trace import Netlist
from faultflow.pdk.roles import LibraryRoles, cell_pin, default_roles, role_cell
from faultflow.wrap.cell import CELL_ATTR
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import WIR_BITS

WSP_ATTR = "faultflow_wsp"  # the cell's part of the WSP
WSP_RECORD_ATTR = "faultflow_wrapper_wsp"  # on the module: the WSP's ports and nets

# The WSP's cells are the library's, by role (faultflow.pdk.roles): and2, and2b, or2,
# xor2, a non-inverting mux2; the WIR's shift stage a plain flop, WBY a reset flop,
# the WIR's update stage a reset flop on WRCK's falling edge.


@dataclass(frozen=True)
class WspPorts:
    """The WSP's port names."""

    wrck: str = "wrck"
    wrstn: str = "wrstn"
    selectwir: str = "selectwir"
    shiftwr: str = "shiftwr"
    capturewr: str = "capturewr"
    updatewr: str = "updatewr"
    wsi: str = "wsi"
    wso: str = "wso"

    def inputs(self) -> tuple[str, ...]:
        return (
            self.wrck,
            self.wrstn,
            self.selectwir,
            self.shiftwr,
            self.capturewr,
            self.updatewr,
            self.wsi,
        )


@dataclass(frozen=True)
class WspNets:
    """The nets the WSP gives the wrapper and the scan pass: WRCK; the boundary
    cells' four control nets; ``serial``, 1 in a serial instruction;
    ``serial_shift``, 1 while a serial instruction shifts the WBR; WSI; WBY's
    output; and the WSO mux, whose A0 :func:`link_wsp` moves."""

    wrck: int
    in_hold: int
    in_safe: int
    out_hold: int
    out_safe: int
    serial: int
    serial_shift: int
    wsi: int
    wby: int
    wso_mux: str
    update: tuple[int, ...]  # the WIR's update stage outputs, bit 0 first


def _cell(
    roles: LibraryRoles,
    cell_role: str,
    role: str,
    inputs: Mapping[str, int | str],
    outputs: Mapping[str, int],
) -> dict[str, Any]:
    """The library's ``cell_role`` cell, part ``role`` of the WSP."""
    return role_cell(roles, cell_role, {WSP_ATTR: role}, inputs, outputs)


def add_wsp(
    cells: dict[str, Any],
    add_input: Callable[[str], int],
    add_output: Callable[[str, int], None],
    net: Callable[[str], int],
    ports: WspPorts = WspPorts(),
    roles: LibraryRoles | None = None,
) -> WspNets:
    """Add the WSP, the WIR, WBY and the decode to a module's ``cells``, of the
    library ``roles`` gives (Sky130 HD by default): its inputs with ``add_input``
    (each one's net), WSO with ``add_output``, every other net with ``net`` (a fresh
    one, named)."""
    roles = roles or default_roles()

    def add(name: str, cell: dict[str, Any]) -> None:
        if name in cells:
            raise WrapError(f"the WSP's cell {name} is already in the netlist")
        cells[name] = cell

    wrck, wrstn, selectwir, shiftwr, capturewr, updatewr, wsi = (
        add_input(name) for name in ports.inputs()
    )

    def gate(name: str, kind: str, role: str, **pins: int | str) -> int:
        found = net(f"__wspn_{name}")
        add(f"__wsp_{name}", _cell(roles, kind, role, pins, {"out": found}))
        return found

    shift_en = gate("shift_en", "and2", "wir", a=selectwir, b=shiftwr)
    update_en = gate("update_en", "and2", "wir", a=selectwir, b=updatewr)
    shift_q = [net(f"__wspn_wir_s{k}") for k in range(WIR_BITS)]
    update_q = [net(f"__wspn_wir_u{k}") for k in range(WIR_BITS)]
    for k in range(WIR_BITS):
        before = wsi if k == 0 else shift_q[k - 1]
        d = gate(f"wir_sd{k}", "mux2", "wir", a0=shift_q[k], a1=before, s=shift_en)
        add(
            f"__wsp_wir_s{k}",
            _cell(roles, "ff", "wir", {"clk": wrck, "d": d}, {"q": shift_q[k]}),
        )
        d = gate(
            f"wir_ud{k}", "mux2", "wir", a0=update_q[k], a1=shift_q[k], s=update_en
        )
        add(
            f"__wsp_wir_u{k}",
            _cell(
                roles,
                "ff_reset_negedge",
                "wir",
                {"clk": wrck, "d": d, "reset_n": wrstn},
                {"q": update_q[k]},
            ),
        )
    u0, u1, u2 = update_q
    # Selected with the WIR deselected: the WBR's (or WBY's) shift and capture.
    dr_shift = gate("dr_shift", "and2b", "decode", a_n=selectwir, b=shiftwr)
    dr_capture = gate("dr_capture", "and2b", "decode", a_n=selectwir, b=capturewr)
    wby_q = net("__wspn_wby")
    kept = gate("wby_keep", "and2b", "wby", a_n=dr_capture, b=wby_q)
    wby_d = gate("wby_d", "mux2", "wby", a0=kept, a1=wsi, s=dr_shift)
    add(
        "__wsp_wby",
        _cell(
            roles,
            "ff_reset",
            "wby",
            {"clk": wrck, "d": wby_d, "reset_n": wrstn},
            {"q": wby_q},
        ),
    )
    # 001 WS_EXTEST and 011 WP_EXTEST; 010 WP_INTEST and 100 WS_INTEST; the serial
    # ones, 001 and 100.
    extest = gate("extest", "and2b", "decode", a_n=u2, b=u0)
    u12 = gate("u12", "xor2", "decode", a=u1, b=u2)
    intest = gate("intest", "and2b", "decode", a_n=u0, b=u12)
    u02 = gate("u02", "xor2", "decode", a=u0, b=u2)
    serial = gate("serial", "and2b", "decode", a_n=u1, b=u02)
    serial_capture = gate("serial_capture", "and2", "decode", a=serial, b=dr_capture)
    serial_hold = gate("serial_hold", "and2b", "decode", a_n=serial_capture, b=serial)
    in_hold = gate("in_hold", "or2", "decode", a=intest, b=serial_hold)
    out_hold = gate("out_hold", "or2", "decode", a=extest, b=serial_hold)
    serial_shift = gate("serial_shift", "and2", "decode", a=serial, b=dr_shift)
    wso = net("__wspn_wso")
    add(
        "__wsp_wso",
        _cell(
            roles,
            "mux2",
            "wso",
            {"a0": wby_q, "a1": shift_q[-1], "s": selectwir},
            {"out": wso},
        ),
    )
    add_output(ports.wso, wso)
    return WspNets(
        wrck=wrck,
        in_hold=in_hold,
        in_safe=extest,
        out_hold=out_hold,
        out_safe=intest,
        serial=serial,
        serial_shift=serial_shift,
        wsi=wsi,
        wby=wby_q,
        wso_mux="__wsp_wso",
        update=tuple(update_q),
    )


def wsp_attribute(ports: WspPorts, nets: WspNets) -> str:
    """The module attribute recording the WSP, for :func:`link_wsp` and the scan
    manifest."""
    return json.dumps({"ports": asdict(ports), "nets": asdict(nets)}, sort_keys=True)


def wsp_of(module: Mapping[str, Any]) -> tuple[WspPorts, WspNets] | None:
    """The WSP ``module`` records, or None."""
    raw = module.get("attributes", {}).get(WSP_RECORD_ATTR)
    if raw is None:
        return None
    data = json.loads(str(raw))
    nets = dict(data["nets"])
    nets["update"] = tuple(nets["update"])
    return WspPorts(**data["ports"]), WspNets(**nets)


def wsp_sites(
    rows: Iterable[Mapping[str, Any]],
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    wrapper: Mapping[str, Any],
) -> frozenset[str]:
    """The fault sites (of ``rows``, the C++ core's list_site_keys of ``module``) of
    the WSP the scan manifest's ``wrapper`` record names: the stem of each of its
    ports' nets -- on a SoC (faultflow.project.soc_wrapper), of each net on the way
    from the SoC's inputs to its blocks' WSPs -- and of each net a WSP cell drives;
    and each branch into a WSP cell, or into a SoC's glue cell on that way. Where
    a control net enters a boundary cell's mux or gate -- its branch there, or its
    stem when that cell alone reads it -- the site is the cell's."""
    wsp = wrapper["wsp"]
    if "blocks" in wrapper:
        nets = {int(net) for net in wsp["path_nets"]}
        cells = {str(name) for name in wsp["path_cells"]}
    else:
        nets = {int(port["net"]) for port in wsp["ports"].values()}
        cells = set()
    cells |= {
        str(name)
        for name, cell in module.get("cells", {}).items()
        if WSP_ATTR in cell.get("attributes", {})
    }
    netlist = Netlist(module, cell_map)
    for net, (driver, _) in netlist.drivers.items():
        if driver in cells:
            nets.add(net)
    readers: dict[int, set[tuple[str, str]]] = {}
    for instance, cell in netlist.cells.items():
        entry = netlist.entry(instance)
        for pin, bits in cell.get("connections", {}).items():
            if netlist.direction(instance, pin, entry) != "output":
                for bit in bits:
                    if isinstance(bit, int):
                        readers.setdefault(bit, set()).add((instance, pin))
    sites: set[str] = set()
    for row in rows:
        if str(row["kind"]) == "branch":
            if str(row.get("consumer_instance", "")) in cells:
                sites.add(str(row["site_key"]))
            continue
        net = int(row["yosys_net_id"])
        only = readers.get(net, set())
        if len(only) == 1 and CELL_ATTR in netlist.cells[next(iter(only))[0]].get(
            "attributes", {}
        ):
            # A control net one boundary cell alone reads: its one site is that
            # cell's input, as each branch of one more cells read would be.
            continue
        if net in nets:
            sites.add(str(row["site_key"]))
    return frozenset(sites)


def _next_net(module: Mapping[str, Any]) -> int:
    used: set[int] = set()
    for port in module.get("ports", {}).values():
        used.update(b for b in port.get("bits", []) if isinstance(b, int))
    for cell in module.get("cells", {}).values():
        for bits in cell.get("connections", {}).values():
            used.update(b for b in bits if isinstance(b, int))
    for entry in module.get("netnames", {}).values():
        used.update(b for b in entry.get("bits", []) if isinstance(b, int))
    return max(used, default=1) + 1


def link_wsp(
    module: dict[str, Any],
    chains: Sequence[Mapping[str, Any]],
    scan_enable: str,
    roles: LibraryRoles | None = None,
) -> None:
    """Give a scanned wrapped module's serial instructions their path (see the module
    docstring): ``chains`` are the scan manifest's, the wrapper's with kind
    "wrapper" and their cells in order; the cells added are of the library ``roles``
    gives (Sky130 HD by default). A module without a WSP is left alone."""
    found = wsp_of(module)
    if found is None:
        return
    roles = roles or default_roles()
    _, nets = found
    cells: dict[str, Any] = module["cells"]
    wrapper = [c for c in chains if c.get("kind") == "wrapper"]
    if not wrapper:
        raise WrapError("the wrapper's flops are on no chain")
    se_bits = module.get("ports", {}).get(scan_enable, {}).get("bits", [])
    if len(se_bits) != 1 or not isinstance(se_bits[0], int):
        raise WrapError(f"no one-bit scan enable port {scan_enable}")
    next_id = _next_net(module)

    def fresh(name: str) -> int:
        nonlocal next_id
        bit = next_id
        next_id += 1
        module.setdefault("netnames", {})[name] = {
            "hide_name": 0,
            "bits": [bit],
            "attributes": {},
        }
        return bit

    def add(name: str, cell: dict[str, Any]) -> None:
        if name in cells:
            raise WrapError(f"the WSP's cell {name} is already in the netlist")
        cells[name] = cell

    wrapper_se = fresh("__wspn_wbr_se")
    add(
        "__wsp_wbr_se",
        _cell(
            roles,
            "or2",
            "scan",
            {"a": se_bits[0], "b": nets.serial_shift},
            {"out": wrapper_se},
        ),
    )
    before = nets.wsi
    for chain in wrapper:
        flops = [str(name) for name in chain.get("cells", [])]
        if not flops:
            raise WrapError(f"wrapper chain {chain.get('index')} has no flops")
        head = cells[flops[0]]["connections"]
        own = head["SDI"][0]
        head_net = fresh(f"__wspn_head_{chain['index']}")
        add(
            f"__wsp_head_{chain['index']}",
            _cell(
                roles,
                "mux2",
                "scan",
                {"a0": own, "a1": before, "s": nets.serial},
                {"out": head_net},
            ),
        )
        head["SDI"] = [head_net]
        for flop in flops:
            cells[flop]["connections"]["SE"] = [wrapper_se]
        before = cells[flops[-1]]["connections"]["Q"][0]
    wbr_out = fresh("__wspn_wbr_out")
    add(
        "__wsp_wbr_out",
        _cell(
            roles,
            "mux2",
            "wso",
            {"a0": nets.wby, "a1": before, "s": nets.serial},
            {"out": wbr_out},
        ),
    )
    wso_mux = cells[nets.wso_mux]
    wso_mux["connections"][cell_pin(wso_mux, "a0", "A0")] = [wbr_out]
