"""EXTEST through the wrapper serial port: WS_EXTEST (``[wrap] extest_instruction =
WS_EXTEST``), the boundary register shifted from WSI to WSO.

A wrapper an IEEE 1500 WSP controls (:mod:`faultflow.wrap.wsp`) has the serial path
in its cells: in a serial instruction each wrapper chain's head takes the tail of
the chain before it (the first takes WSI), the wrapper flops shift while ShiftWR is
1 with the WIR deselected, and WSO is the last chain's tail; every boundary cell
holds but while CaptureWR is 1, when the cells the mode observes capture
(:mod:`faultflow.wrap.wsp_model`). On a SoC the blocks' WSI and WSO are chained, so
the boundary register runs through every block's, WSI to WSO.

:func:`serial_manifest` turns a chip's scan manifest -- a block's or a SoC's -- into
the one a WS_EXTEST test shifts: one chain from WSI to WSO, scan enable ShiftWR,
clock WRCK. The chain isn't taken from the records: it is traced on the chip's cells
from WSO back to WSI, through the gates the test's held inputs and the WIRs'
instruction keep passing it on (:meth:`faultflow.control_trace.Netlist.through`),
each WIR's update stage at WS_EXTEST's code, as the test setup program leaves it. So
a serial path the cells don't have is refused before a test runs on it. Every
wrapper flop must sit on the chain once, and no core flop; each one's scan enable
must come from ShiftWR and its clock from WRCK, neither inverted.

The test (:data:`SERIAL` in the manifest) holds WRSTN at 1, SelectWIR and UpdateWR at
0, the chip's own scan enable at 0 (the core chains don't shift) and each wrapper
chain's own scan input at 0; CaptureWR is 1 at the capture and 0 while the chain
shifts (``shift_pi_values``). It reads the boundary register at WSO alone: the
wrapper chains' own scan outputs aren't observed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.control_trace import Netlist, one_net, port_bits
from faultflow.scan.stitch import SCAN_CELL_TYPES
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp import WSP_ATTR
from faultflow.wrap.wsp_model import OPCODES, WIR_BITS
from faultflow.wrap.wsp_setup import HELD, access_of

INSTRUCTION = "WS_EXTEST"
SERIAL = "serial"  # the serial manifest's record of its access
WIR_UPDATE = "__wsp_wir_u{k}"  # a WSP's WIR update flops (faultflow.wrap.wsp)


def serial_extest(cfg: Any) -> bool:
    """Whether the config's EXTEST runs in WS_EXTEST."""
    return str(cfg.wrap.extest_instruction) == INSTRUCTION


def is_serial(manifest: Mapping[str, Any]) -> bool:
    """Whether `manifest` is a serial manifest (:func:`serial_manifest`)."""
    return isinstance(manifest.get(SERIAL), dict)


def _wso_port(wrapper: Mapping[str, Any]) -> str:
    wsp = wrapper["wsp"]
    return str(wsp["wso"] if "blocks" in wrapper else wsp["ports"]["wso"]["port"])


def _prefixes(wrapper: Mapping[str, Any]) -> list[str]:
    """The name prefix of each WSP's cells on the chip, WSI to WSO: a block's own
    (none), or each SoC block instance's (faultflow.project.assemble names a
    block's cells ``<instance>__<cell>``)."""
    if "blocks" in wrapper:
        return [f"{instance}__" for instance in wrapper["wsp"]["chain"]]
    return [""]


def wir_state(
    module: Mapping[str, Any], wrapper: Mapping[str, Any], instruction: str
) -> dict[int, int]:
    """Each WIR update flop's output on the chip, at `instruction`'s code."""
    code = OPCODES[instruction]
    cells = module.get("cells", {})
    known: dict[int, int] = {}
    for prefix in _prefixes(wrapper):
        for k in range(WIR_BITS):
            name = prefix + WIR_UPDATE.format(k=k)
            cell = cells.get(name)
            if cell is None or cell.get("attributes", {}).get(WSP_ATTR) != "wir":
                raise WrapError(f"the WSP has no WIR update flop {name}")
            q = one_net(cell.get("connections", {}).get("Q"))
            if not isinstance(q, int):
                raise WrapError(f"the WIR update flop {name} has no Q net")
            known[q] = (code >> k) & 1
    return known


def _set(values: dict[str, int], port: str, value: int) -> None:
    if values.setdefault(port, value) != value:
        raise WrapError(
            f"chip input {port} would have to be held at 0 and at 1 in WS_EXTEST"
        )


def serial_access(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """What a WS_EXTEST test holds (capture values), the inputs it sets otherwise
    while the chain shifts, and its ports (wsi, wso, shift, wrck), for the chip
    `manifest` records."""
    wrapper = manifest.get("wrapper")
    if not isinstance(wrapper, dict):
        raise WrapError("WS_EXTEST needs the wrapper ff.py wrap puts on; there is none")
    if wrapper.get("control") != "wsp":
        raise WrapError(
            "WS_EXTEST needs a wrapper an IEEE 1500 WSP controls ([wrap] control = "
            "wsp): mode pins have no serial port to shift the boundary register "
            "through"
        )
    access = access_of(wrapper)
    for role in ("shiftwr", "wsi"):
        port, inverted = access.pins[role]
        if inverted:
            raise WrapError(
                f"WS_EXTEST shifts through {role}: chip input {port} reaches it "
                "through an inverter"
            )
    holds: dict[str, int] = {}
    for role in ("wrstn", "selectwir", "updatewr"):
        port, inverted = access.pins[role]
        _set(holds, port, HELD[role] ^ int(inverted))
    capture_port, capture_inverted = access.pins["capturewr"]
    _set(holds, capture_port, 1 ^ int(capture_inverted))
    _set(holds, str(manifest["scan_enable"]), 0)
    for chain in manifest.get("chains", []):
        if chain.get("kind") == "wrapper":
            _set(holds, str(chain["scan_in"]), 0)
    ports = {
        "wsi": access.pins["wsi"][0],
        "wso": _wso_port(wrapper),
        "shift": access.pins["shiftwr"][0],
        "wrck": access.wrck,
    }
    for role in ("wsi", "shift", "wrck"):
        if ports[role] in holds:
            raise WrapError(f"WS_EXTEST would hold its {role} input {ports[role]}")
    return holds, {capture_port: int(capture_inverted)}, ports


def trace_serial_chain(
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    holds: Mapping[str, int],
    known: Mapping[int, int],
    wsi: str,
    wso: str,
) -> list[str]:
    """The scan cells from `wsi` to `wso` on `module`'s cells, the one nearest WSI
    first: traced back from WSO through the gates `holds` (input ports) and
    `known` (nets) keep passing it on, each cell's SDI to the cell before."""
    netlist = Netlist(module, cell_map)
    inputs = dict(port_bits(module, "input"))
    outputs = dict(port_bits(module, "output"))
    if wsi not in inputs or wso not in outputs:
        raise WrapError(f"the chip has no one-bit input {wsi} and output {wso}")
    cells = netlist.cells
    scan_cells = {n for n, c in cells.items() if str(c.get("type")) in SCAN_CELL_TYPES}

    def back(net: Any, what: str) -> int | str:
        """The scan cell (its name) or WSI (its net) `net` comes from."""
        found = netlist.through(net, holds, known)
        if found.inverted:
            raise WrapError(f"WS_EXTEST's path to {what} inverts")
        if found.source == inputs[wsi]:
            return int(found.source)
        driver = (
            netlist.drivers.get(found.source) if isinstance(found.source, int) else None
        )
        if driver is None or driver[0] not in scan_cells or driver[1] != "Q":
            raise WrapError(
                f"WS_EXTEST's path to {what} stops at net {found.source}, neither "
                f"{wsi} nor a scan cell's Q: the WIR's WS_EXTEST doesn't open it"
            )
        return str(driver[0])

    chain: list[str] = []
    current = back(outputs[wso], wso)
    while isinstance(current, str):
        if current in chain:
            raise WrapError(f"WS_EXTEST's path runs round through {current}")
        chain.append(current)
        sdi = cells[current].get("connections", {}).get("SDI")
        current = back(one_net(sdi), f"{current}'s scan input")
    return chain[::-1]


def serial_manifest(
    manifest: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    module: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The scan manifest a WS_EXTEST test of the chip `manifest` records shifts
    (see the module docstring), traced on `module` -- the chip's top module, read
    from the manifest's netlist by default."""
    if is_serial(manifest):
        return dict(manifest)
    if module is None:
        data = json.loads(Path(str(manifest["generic_json"])).read_text("utf-8"))
        module = data["modules"][str(manifest["top"])]
    holds, shift_values, ports = serial_access(manifest)
    wrapper = manifest["wrapper"]
    known = wir_state(module, wrapper, INSTRUCTION)
    cells = trace_serial_chain(
        module, cell_map, holds, known, ports["wsi"], ports["wso"]
    )
    wrapper_flops = {
        str(name)
        for chain in manifest.get("chains", [])
        if chain.get("kind") == "wrapper"
        for name in chain.get("cells", [])
    }
    stray = sorted(set(cells) - wrapper_flops)
    missing = sorted(wrapper_flops - set(cells))
    if stray or missing:
        raise WrapError(
            "WS_EXTEST's path from "
            f"{ports['wsi']} to {ports['wso']} isn't the wrapper flops: "
            + (f"not a wrapper flop {stray[:5]} " if stray else "")
            + (f"off it {missing[:5]}" if missing else "")
        )
    netlist = Netlist(module, cell_map)
    inputs = dict(port_bits(module, "input"))
    records = {str(r["instance"]): r for r in manifest["cells"]}
    clocks = dict(
        zip(
            [int(n) for n in manifest["clock_nets"]],
            [str(p) for p in manifest["clock_ports"]],
        )
    )
    rows: list[dict[str, Any]] = []
    for position, name in enumerate(cells):
        conns = module["cells"][name]["connections"]
        enable = netlist.through(one_net(conns.get("SE")), holds, known)
        if enable.source != inputs[ports["shift"]] or enable.inverted:
            raise WrapError(
                f"wrapper flop {name}'s scan enable doesn't come from "
                f"{ports['shift']} in WS_EXTEST"
            )
        record = records[name]
        if clocks.get(int(record["clock_net"])) != ports["wrck"]:
            raise WrapError(f"wrapper flop {name} isn't clocked by {ports['wrck']}")
        rows.append({**record, "chain_index": 0, "chain_position": position})
    observed_not = [
        str(chain["scan_out"])
        for chain in manifest.get("chains", [])
        if chain.get("kind") == "wrapper"
    ]
    clock_ports = sorted({clocks[int(r["clock_net"])] for r in rows})
    paths = manifest.get("clock_paths") or {}
    chain = {
        "index": 0,
        "kind": "wrapper",
        "scan_in": ports["wsi"],
        "scan_in_net": inputs[ports["wsi"]],
        "scan_out": ports["wso"],
        "scan_out_net": dict(port_bits(module, "output"))[ports["wso"]],
        "length": len(rows),
        "cells": list(cells),
        "cell_records": rows,
    }
    return {
        **manifest,
        "chain_count": 1,
        "cell_count": len(rows),
        "clock_nets": sorted({int(r["clock_net"]) for r in rows}),
        "clock_ports": clock_ports,
        "clock_paths": {p: paths[p] for p in clock_ports if p in paths},
        "scan_inputs": [ports["wsi"]],
        "scan_outputs": [ports["wso"]],
        "scan_enable": ports["shift"],
        "max_chain_length": len(rows),
        "min_chain_length": len(rows),
        "chains": [chain],
        "cells": rows,
        "ineligible_ffs": [
            item
            for item in manifest.get("ineligible_ffs", [])
            if isinstance(item, dict) and item.get("reason") == "wsp"
        ],
        SERIAL: {
            "instruction": INSTRUCTION,
            "holds": holds,
            "shift_values": shift_values,
            "unobserved": observed_not,
            # The chip's parallel access, which WP_EXTEST and the other modes shift
            # through: its scan path still, held here (site_resolution).
            "parallel_scan_enable": str(manifest["scan_enable"]),
            "parallel_scan_inputs": [
                str(c["scan_in"])
                for c in manifest.get("chains", [])
                if c.get("kind") == "wrapper"
            ],
        },
    }


def for_patterns(
    manifest: Mapping[str, Any],
    patterns: Sequence[Mapping[str, Any]],
    cell_map: Mapping[str, Any],
) -> dict[str, Any]:
    """The manifest exported `patterns` are applied with: the chip's, or its
    serial one when they are WS_EXTEST's (``access``). Every pattern must agree."""
    accesses = {str(pattern.get("access", "")) for pattern in patterns}
    if len(accesses) > 1:
        raise WrapError(
            f"the patterns mix accesses {sorted(a or 'scan' for a in accesses)}"
        )
    if accesses == {INSTRUCTION}:
        return serial_manifest(manifest, cell_map)
    if accesses - {""}:
        raise WrapError(f"no pattern access {sorted(accesses)}")
    return dict(manifest)
