"""The scan manifest's record of an IEEE 1500 wrapper (faultflow.wrap.block): how its
mode is set -- its mode pins, or its WSP (faultflow.wrap.wsp): the WSP's ports, the
nets its decode drives and the instruction codes -- its clock, and each boundary cell
-- its port bit, its cells, the nets on its system side and its core side, and where
its flop sits on the wrapper chains. Built from the scanned netlist, where the
wrapper's cells carry their attributes."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from dataclasses import asdict

from faultflow.scan.setup import SetupCycle
from faultflow.wrap.block import CLOCK_ATTR, CONTROL_ATTR, EXTEST_ATTR, INTEST_ATTR
from faultflow.wrap.cell import CELL_ATTR, INDEX_ATTR, INPUT, ROLE_ATTR, SIDE_ATTR
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp import wsp_of
from faultflow.wrap.wsp_model import OPCODES
from faultflow.wrap.wsp_setup import access_of, holds, mode_program

VERSION = "faultflow_wrapper_v1"
MODES = {"functional": (0, 0), "intest": (1, 0), "extest": (0, 1)}


def _port(module: Mapping[str, Any], name: str) -> dict[str, Any]:
    bits = module.get("ports", {}).get(name, {}).get("bits", [])
    if len(bits) != 1 or not isinstance(bits[0], int):
        raise WrapError(f"the wrapper's port {name} isn't a one-bit port")
    return {"port": name, "net": bits[0]}


def _split(label: str) -> tuple[str, int]:
    if label.endswith("]") and "[" in label:
        port, bit = label[:-1].rsplit("[", 1)
        return port, int(bit)
    return label, 0


def wrapper_record(
    module: Mapping[str, Any], chains: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """``module``'s wrapper, or None without one. ``chains``: the scan manifest's,
    with their cell records."""
    attributes = module.get("attributes", {})
    control = attributes.get(CONTROL_ATTR)
    if control is None:
        return None
    position: dict[str, tuple[int, int]] = {}
    for chain in chains:
        for cell_record in chain.get("cell_records", []):
            position[str(cell_record["instance"])] = (
                int(cell_record["chain_index"]),
                int(cell_record["chain_position"]),
            )
    groups: dict[str, dict[str, Any]] = {}
    for instance, cell in module.get("cells", {}).items():
        cell_attributes = cell.get("attributes", {})
        label = cell_attributes.get(CELL_ATTR)
        if label is None:
            continue
        group = groups.setdefault(
            str(label),
            {
                "label": str(label),
                "side": str(cell_attributes[SIDE_ATTR]),
                "index": int(str(cell_attributes[INDEX_ATTR])),
            },
        )
        group[str(cell_attributes[ROLE_ATTR])] = instance
    cells: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda g: int(g["index"])):
        if any(role not in group for role in ("mux", "gate", "ff")):
            raise WrapError(f"the boundary cell on {group['label']} is incomplete")
        if group["ff"] not in position:
            raise WrapError(f"the boundary cell on {group['label']} isn't on a chain")
        connections = module["cells"]
        cfi = connections[group["mux"]]["connections"]["A0"][0]
        out = connections[group["gate"]]["connections"]["X"][0]
        port, bit = _split(group["label"])
        chain_index, place = position[group["ff"]]
        sys_net, core_net = (cfi, out) if group["side"] == INPUT else (out, cfi)
        cells.append(
            {
                **group,
                "port": port,
                "bit": bit,
                "sys_net": sys_net,
                "core_net": core_net,
                "chain": chain_index,
                "position": place,
            }
        )
    record: dict[str, Any] = {"version": VERSION, "control": str(control)}
    if control == "wsp":
        found = wsp_of(module)
        if found is None:
            raise WrapError("the wrapper's WSP isn't recorded on the module")
        ports, nets = found
        record["wsp"] = {
            "ports": {
                role: _port(module, name) for role, name in asdict(ports).items()
            },
            "nets": {
                "in_hold": nets.in_hold,
                "in_safe": nets.in_safe,
                "out_hold": nets.out_hold,
                "out_safe": nets.out_safe,
                "serial": nets.serial,
                "serial_shift": nets.serial_shift,
                "update": list(nets.update),
            },
            "opcodes": dict(OPCODES),
        }
    else:
        record["intest"] = _port(module, str(attributes[INTEST_ATTR]))
        record["extest"] = _port(module, str(attributes[EXTEST_ATTR]))
    record["clock"] = _port(module, str(attributes[CLOCK_ATTR]))
    record["cells"] = cells
    return record


def mode_holds(manifest: Mapping[str, Any], mode: str) -> dict[str, int]:
    """The inputs a scan test in ``mode`` holds for the wrapper, and their values:
    its mode pins -- or, with a WSP, the WSP's inputs, the same in every mode
    (faultflow.wrap.wsp_setup: the instruction sets the mode) -- none without a
    wrapper. A SoC's record (faultflow.project.soc_wrapper) names the SoC inputs
    that set its blocks' mode pins, per mode."""
    wrapper = manifest.get("wrapper")
    if not isinstance(wrapper, dict):
        return {}
    if mode not in MODES:
        raise WrapError(f"no wrapper mode {mode!r}")
    if wrapper.get("control") == "wsp":
        return holds(access_of(wrapper))
    if "holds" in wrapper:
        return {str(port): int(value) for port, value in wrapper["holds"][mode].items()}
    intest, extest = MODES[mode]
    return {wrapper["intest"]["port"]: intest, wrapper["extest"]["port"]: extest}


def mode_setup(manifest: Mapping[str, Any], mode: str) -> tuple[SetupCycle, ...]:
    """The test setup program a scan test in ``mode`` starts with for the
    wrapper: a WSP's, loading the mode's instruction (faultflow.wrap.wsp_setup);
    none for mode pins, or without a wrapper."""
    wrapper = manifest.get("wrapper")
    if not isinstance(wrapper, dict) or wrapper.get("control") != "wsp":
        return ()
    if mode not in MODES:
        raise WrapError(f"no wrapper mode {mode!r}")
    return mode_program(wrapper, mode)


def serial_nets(wrapper: Mapping[str, Any]) -> dict[int, int]:
    """A WSP's nets that a scan test, its instruction parallel or WS_BYPASS, holds
    at 0 -- each WSP's ``serial`` and ``serial_shift`` (faultflow.wrap.wsp), every
    block's on a SoC -- each at 0: the serial paths through the wrapper chains'
    heads and beside the scan enable closed. None for mode pins."""
    if wrapper.get("control") != "wsp":
        return {}
    wsp = wrapper["wsp"]
    if "blocks" in wrapper:
        return {int(net): 0 for net in wsp["serial_nets"]}
    return {int(wsp["nets"]["serial"]): 0, int(wsp["nets"]["serial_shift"]): 0}


def mode_nets(wrapper: Mapping[str, Any]) -> dict[int, int]:
    """Each net that sets the boundary cells' mode, and the value INTEST holds it
    at: a block's two mode nets -- or the four its WSP's decode drives -- or every
    net on a SoC's paths from its inputs to its blocks' mode pins (with WSPs, its
    blocks' decode nets)."""
    if "mode_nets" in wrapper:
        return {int(net): int(value) for net, value in wrapper["mode_nets"].items()}
    if wrapper.get("control") == "wsp":
        nets = wrapper["wsp"]["nets"]
        return {
            int(nets["in_hold"]): 1,
            int(nets["in_safe"]): 0,
            int(nets["out_hold"]): 0,
            int(nets["out_safe"]): 1,
        }
    return {int(wrapper["intest"]["net"]): 1, int(wrapper["extest"]["net"]): 0}
