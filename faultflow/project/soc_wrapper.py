"""The IEEE 1500 wrappers of a composed SoC, as one record: every block's boundary
cells under their names and nets in the SoC, and how the SoC's own inputs set each
block's mode.

Each block's wrapper record (faultflow.wrap.record) names the block's own cells and
nets; composing the SoC (faultflow.project.assemble) renames the cells
``<instance>__<cell>`` and remaps the nets. The glue must bring each block's two mode
pins to SoC inputs through buffers and inverters alone, so a mode is a set of held
SoC inputs: each block's pins at the mode's values, an inverter on the way flipping
its input's. Two blocks sharing an input must want it at one value.

The record keeps what the wrapper modes need, as a block's does: the cells (each
with its block), and per net on a mode pin's path the value INTEST holds it at
(``mode_nets``), which decides which mode owns a stuck-at on it
(faultflow.wrap.sides); and per mode the SoC inputs it holds (``holds``).

Blocks an IEEE 1500 WSP controls (``[wrap] control = wsp``, faultflow.wrap.wsp)
share one WSP on the SoC (:func:`_wsp_record`): each of WRCK, WRSTN, SelectWIR,
ShiftWR, CaptureWR and UpdateWR comes from one SoC input, for every block alike,
through buffers and inverters (WRCK through buffers alone); and their WSI and WSO
form one chain through buffers, from a SoC input through every block to a SoC
output. A mode is then an instruction in every WIR along it
(faultflow.wrap.wsp_setup), and its mode nets are the blocks' decode nets. The
blocks' wrappers are all set one way or all the other.
"""

from __future__ import annotations

from typing import Any, Mapping

from faultflow.control_trace import Netlist, Trace, port_bits
from faultflow.project.manifest import ProjectError
from faultflow.wrap.record import MODES
from faultflow.wrap.wsp import WSP_ATTR

SOC_VERSION = "faultflow_soc_wrapper_v1"
# The WSP inputs a SoC shares among its blocks, WRCK first; WSI is chained.
SHARED = ("wrck", "wrstn", "selectwir", "shiftwr", "capturewr", "updatewr")


def _net(remap: Mapping[int, int | str], value: Any) -> Any:
    return remap.get(value, value) if isinstance(value, int) else value


def _cells(
    instance: str,
    name: str,
    record: Mapping[str, Any],
    remap: Mapping[int, int | str],
    place: Mapping[str, tuple[int, int]],
) -> list[dict[str, Any]]:
    """`instance`'s boundary cells, under their names and nets in the SoC."""
    cells: list[dict[str, Any]] = []
    for cell in record["cells"]:
        ff = f"{instance}__{cell['ff']}"
        if ff not in place:
            raise ProjectError(f"block {name}'s wrapper flop {ff} is on no chain")
        chain, position = place[ff]
        cells.append(
            {
                **cell,
                "block": name,
                "label": f"{name}/{cell['label']}",
                "mux": f"{instance}__{cell['mux']}",
                "gate": f"{instance}__{cell['gate']}",
                "ff": ff,
                "sys_net": _net(remap, cell["sys_net"]),
                "core_net": _net(remap, cell["core_net"]),
                "chain": chain,
                "position": position,
            }
        )
    return cells


def soc_wrapper(
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    blocks: Mapping[str, tuple[str, Mapping[str, Any]]],
    remaps: Mapping[str, Mapping[int, int | str]],
    chains: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """The SoC's wrapper record. `blocks`: instance -> (block name, the block's
    wrapper record); `remaps`: instance -> block net -> SoC net (compose_soc);
    `chains`: the SoC scan manifest's, with their cell records."""
    netlist = Netlist(module, cell_map)
    place = {
        str(r["instance"]): (int(r["chain_index"]), int(r["chain_position"]))
        for chain in chains
        for r in chain.get("cell_records", [])
    }
    controls = {str(record.get("control", "pins")) for _, record in blocks.values()}
    if len(controls) > 1:
        raise ProjectError(
            "the SoC's blocks' wrappers are set some by mode pins and some by an "
            "IEEE 1500 WSP: wrap them all one way ([wrap] control)"
        )
    cells: list[dict[str, Any]] = []
    for instance, (name, record) in blocks.items():
        cells += _cells(instance, name, record, remaps[instance], place)
    if controls == {"wsp"}:
        return _wsp_record(module, netlist, blocks, remaps, cells)
    mode_nets: dict[int, int] = {}
    holds: dict[str, dict[str, int]] = {mode: {} for mode in MODES}
    records: list[dict[str, Any]] = []
    for instance, (name, record) in blocks.items():
        remap = remaps[instance]
        pins: dict[str, dict[str, Any]] = {}
        first = record["cells"][0]
        for role, intest_value in (("intest", 1), ("extest", 0)):
            net = _net(remap, record[role]["net"])
            # A cell reading the pin: an input cell's mux selects on INTEST and
            # its gate makes it safe on EXTEST; an output cell's, the reverse.
            selects = (first["side"] == "input") == (role == "intest")
            reader, pin = (first["mux"], "S") if selects else (first["gate"], "A_N")
            trace = (
                netlist.trace(net, f"{instance}__{reader}", pin)
                if isinstance(net, int)
                else None
            )
            if trace is None or trace.port is None:
                raise ProjectError(
                    f"block {name}'s {role} pin isn't driven from a SoC input "
                    "through buffers and inverters"
                )
            # The value at the block's pin in each mode, and at the SoC input.
            for mode, (intest, extest) in MODES.items():
                wanted = intest if role == "intest" else extest
                at_port = trace.value_at_pin(wanted)
                held = holds[mode].setdefault(trace.port, at_port)
                if held != at_port:
                    raise ProjectError(
                        f"SoC input {trace.port} sets the mode pins of blocks that "
                        f"need it at 0 and at 1 in {mode}"
                    )
            for step_net, _consumer, _pin, inverted in trace.steps:
                mode_nets[step_net] = intest_value ^ int(inverted)
            pins[role] = {"port": trace.port, "net": net}
        records.append({"block": name, "instance": instance, **pins})
    return {
        "version": SOC_VERSION,
        "control": "pins",
        "blocks": records,
        "mode_nets": {str(net): value for net, value in sorted(mode_nets.items())},
        "holds": holds,
        "cells": cells,
    }


def _wsp_record(
    module: Mapping[str, Any],
    netlist: Netlist,
    blocks: Mapping[str, tuple[str, Mapping[str, Any]]],
    remaps: Mapping[str, Mapping[int, int | str]],
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """The record of a SoC whose blocks' WSPs share its inputs and chain their
    WSI and WSO (see the module docstring): per WSP input the SoC input that
    drives it (``pins``; WRCK's, ``wrck``), the WIRs from WSI to WSO (``chain``,
    by instance), the SoC output WSO reaches, the nets and glue cells on the way
    (``path_nets``, ``path_cells``: the WSP's, on the SoC), the blocks' serial
    nets, 0 in every scan test (``serial_nets``), and their decode nets as the
    mode nets."""
    readers: dict[int, list[tuple[str, str]]] = {}
    for instance, cell in netlist.cells.items():
        entry = netlist.entry(instance)
        for pin, bits in cell.get("connections", {}).items():
            if netlist.direction(instance, pin, entry) != "output":
                for bit in bits:
                    if isinstance(bit, int):
                        readers.setdefault(bit, []).append((instance, str(pin)))
    pins: dict[str, dict[str, Any]] = {}
    path_nets: set[int] = set()
    path_cells: set[str] = set()
    fed_by: dict[str, str | int] = {}  # instance -> a SoC input's name, or a net
    wso_of: dict[int, str] = {}  # a block's WSO net in the SoC -> its instance
    mode_nets: dict[int, int] = {}
    serial_nets: set[int] = set()
    records: list[dict[str, Any]] = []
    for instance, (name, record) in blocks.items():
        remap = remaps[instance]
        wsp = record["wsp"]
        own = {
            cell
            for cell in netlist.cells
            if cell.startswith(f"{instance}__")
            and WSP_ATTR in netlist.cells[cell].get("attributes", {})
        }

        def traced(role: str) -> Trace:
            """The way to `role`'s WSP input of this block from where it starts,
            its nets and glue cells kept as the WSP's."""
            net = _net(remap, wsp["ports"][role]["net"])
            reader = next(((c, p) for c, p in readers.get(net, []) if c in own), None)
            if reader is None:
                raise ProjectError(
                    f"block {name}'s WSP input {wsp['ports'][role]['port']} reaches "
                    "none of its WSP's cells"
                )
            trace = netlist.trace(net, *reader)
            for step_net, consumer, _pin, _inverted in trace.steps:
                path_nets.add(step_net)
                if consumer not in own:
                    path_cells.add(consumer)
            return trace

        for role in SHARED:
            trace = traced(role)
            if trace.port is None or (role == "wrck" and trace.inverted):
                raise ProjectError(
                    f"block {name}'s WSP input {wsp['ports'][role]['port']} isn't "
                    "driven from a SoC input through buffers"
                    + (" alone" if role == "wrck" else " and inverters")
                )
            found = {"port": trace.port, "inverted": bool(trace.inverted)}
            if pins.setdefault(role, found) != found:
                raise ProjectError(
                    f"the blocks' WSP inputs {role} come from SoC inputs "
                    f"{pins[role]['port']} and {trace.port}, or one through an "
                    "inverter: a SoC's blocks share their WSP's"
                )
        trace = traced("wsi")
        if trace.inverted:
            raise ProjectError(f"block {name}'s WSI comes through an inverter")
        fed_by[instance] = trace.port if trace.port is not None else trace.steps[-1][0]
        wso_of[int(_net(remap, wsp["ports"]["wso"]["net"]))] = instance
        nets = wsp["nets"]
        for key, intest_value in (
            ("in_hold", 1),
            ("in_safe", 0),
            ("out_hold", 0),
            ("out_safe", 1),
        ):
            mode_nets[int(_net(remap, nets[key]))] = intest_value
        serial_nets |= {int(_net(remap, nets[k])) for k in ("serial", "serial_shift")}
        records.append({"block": name, "instance": instance})
    chain, wsi = _wsi_chain(fed_by, wso_of)
    wso, steps = _soc_wso(module, netlist, wso_of, chain[-1])
    path_nets |= {net for net, _ in steps}
    path_cells |= {cell for _, cell in steps}
    pins["wsi"] = {"port": wsi, "inverted": False}
    wrck = str(pins.pop("wrck")["port"])
    return {
        "version": SOC_VERSION,
        "control": "wsp",
        "blocks": records,
        "wsp": {
            "pins": pins,
            "wrck": wrck,
            "wso": wso,
            "chain": chain,
            "path_nets": sorted(path_nets),
            "path_cells": sorted(path_cells),
            "serial_nets": sorted(serial_nets),
        },
        "mode_nets": {str(net): value for net, value in sorted(mode_nets.items())},
        "cells": cells,
    }


def _wsi_chain(
    fed_by: Mapping[str, str | int], wso_of: Mapping[int, str]
) -> tuple[list[str], str]:
    """The blocks from WSI to WSO, by instance, and the SoC input feeding the
    first: `fed_by` gives each block's WSI source -- a SoC input's name, or a net
    that must be another block's WSO (`wso_of`)."""
    heads = sorted(i for i, source in fed_by.items() if isinstance(source, str))
    if len(heads) != 1:
        raise ProjectError(
            "one block's WSI comes from a SoC input, every other's from a block's "
            f"WSO, through buffers; from SoC inputs: {heads or 'none'}"
        )
    following: dict[str, str] = {}
    for instance, source in fed_by.items():
        if isinstance(source, str):
            continue
        before = wso_of.get(int(source))
        if before is None:
            raise ProjectError(
                f"{instance}'s WSI comes from neither a SoC input nor a block's WSO "
                "through buffers"
            )
        if before in following:
            raise ProjectError(f"{before}'s WSO feeds the WSI of more than one block")
        following[before] = instance
    chain = [heads[0]]
    while chain[-1] in following and len(chain) <= len(fed_by):
        chain.append(following[chain[-1]])
    if len(chain) != len(fed_by):
        raise ProjectError(
            "the blocks' WSI and WSO don't form one chain from a SoC input: "
            f"{' -> '.join(chain)}"
        )
    return chain, str(fed_by[heads[0]])


def _soc_wso(
    module: Mapping[str, Any],
    netlist: Netlist,
    wso_of: Mapping[int, str],
    last: str,
) -> tuple[str, list[tuple[int, str]]]:
    """The SoC output the last block's WSO reaches through buffers, and the
    (net, buffer) steps on the way."""
    target = next(net for net, instance in wso_of.items() if instance == last)
    found: list[tuple[str, list[tuple[int, str]]]] = []
    for name, net in port_bits(module, "output"):
        steps: list[tuple[int, str]] = []
        current: Any = net
        while isinstance(current, int) and current != target:
            driver = netlist.drivers.get(current)
            entry = netlist.entry(driver[0]) if driver is not None else None
            if driver is None or (entry or {}).get("gate_type") != "BUF":
                break
            steps.append((current, driver[0]))
            current = next(
                bits[0]
                for pin, bits in netlist.cells[driver[0]]["connections"].items()
                if netlist.direction(driver[0], pin, entry) != "output"
            )
        if current == target:
            found.append((name, steps))
    if len(found) != 1:
        raise ProjectError(
            f"the last block's ({last}) WSO reaches {len(found)} SoC outputs "
            "through buffers, not one"
        )
    return found[0]
