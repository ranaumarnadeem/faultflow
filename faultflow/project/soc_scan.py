"""Generate a SoC-level scan manifest for a composed EXTEST graybox.

A hierarchical SoC EXTEST graybox (see faultflow/project/assemble.py's graybox
compose) has each block's IEEE-1500 WBR scan cells daisy-chained through the SoC
glue (``soc_wbr_si -> ... -> soc_wbr_so``) plus interconnect logic, and NO
internal scan FFs. To run ``run_atpg -scan`` in EXTEST mode on it, the runner's
``_preflight_sim_scan`` needs a scan manifest with a PASS ``latest_check`` and a
matching hash, and that manifest must pass ``check_scan`` -- which validates the
wrapper chain's CTI->CTO connectivity against the manifest's cell order.

This module builds that manifest by *tracing* the physical chain from the SoC
scan-in port (robust to the fact that each block's WBR ``faultflow_wbr_chain`` /
``wbr_bit`` attributes collide after composition -- every block restarts at chain
"0", bit 0). No netlist mutation and no re-tagging is required: EXTEST fusion
(``fuse_wbr_into_view``) treats each wrapper cell as an independent combinational
boundary port, so wrapper-cell shift ORDER is irrelevant to the EXTEST ATPG --
the trace is used only to produce a manifest ``check_scan`` accepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError
from faultflow.scan.reports import hash_file, utc_timestamp
from faultflow.scan.stitch import _load_json, _top_module
from faultflow.scan.wbr_view import _WBR_SCAN_IN_TYPES, _WBR_SCAN_OUT_TYPES

_WBR_SCAN_CELL_TYPES = _WBR_SCAN_IN_TYPES | _WBR_SCAN_OUT_TYPES


def _one_bit(cell: dict[str, Any], pin: str, instance: str) -> int:
    conns = cell.get("connections", {})
    bits = conns.get(pin) if isinstance(conns, dict) else None
    if not isinstance(bits, list) or len(bits) != 1 or not isinstance(bits[0], int):
        raise ScanError(f"{instance}.{pin} must be one concrete net")
    return int(bits[0])


def _port_net(module: dict[str, Any], name: str, direction: str) -> int:
    ports = module.get("ports", {})
    port = ports.get(name) if isinstance(ports, dict) else None
    if not isinstance(port, dict) or port.get("direction") != direction:
        raise ScanError(f"missing {direction} port {name!r}")
    bits = port.get("bits")
    if not isinstance(bits, list) or len(bits) != 1 or not isinstance(bits[0], int):
        raise ScanError(f"port {name!r} must be one concrete net")
    return int(bits[0])


def trace_wrapper_chain(module: dict[str, Any], soc_wbr_si_net: int) -> list[str]:
    """Return WBR scan-cell instance names in physical chain order (CTI->CTO),
    starting from the cell whose CTI is driven by ``soc_wbr_si_net``.

    Follows the same net-walk ``check_scan`` uses to validate connectivity, so a
    trace that succeeds here is exactly one ``check_scan`` will accept.
    """
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return []
    node_by_cti: dict[int, str] = {}
    cto_by_node: dict[str, int] = {}
    for inst, cell in cells.items():
        if not isinstance(cell, dict) or cell.get("type") not in _WBR_SCAN_CELL_TYPES:
            continue
        inst = str(inst)
        cti = _one_bit(cell, "CTI", inst)
        cto = _one_bit(cell, "CTO", inst)
        if cti in node_by_cti:
            raise ScanError(f"WBR cells share CTI net {cti}")
        node_by_cti[cti] = inst
        cto_by_node[inst] = cto

    ordered: list[str] = []
    cur = soc_wbr_si_net
    while cur in node_by_cti:
        inst = node_by_cti[cur]
        if inst in ordered:
            raise ScanError(f"cycle in wrapper chain at {inst}")
        ordered.append(inst)
        cur = cto_by_node[inst]

    unreached = set(cto_by_node) - set(ordered)
    if unreached:
        raise ScanError(
            "WBR cells not reachable from soc scan-in (chain not daisy-chained "
            f"through the glue?): {sorted(unreached)}"
        )
    return ordered


def build_soc_scan_manifest(
    soc_json_path: Path,
    top: str,
    *,
    soc_wbr_si: str,
    soc_wbr_so: str,
    soc_wbr_se: str,
    clock_port: str,
    techmap_verilog: str = "",
) -> dict[str, Any]:
    """Trace the SoC wrapper chain and build a v2 scan manifest for EXTEST.

    The manifest has ZERO internal ``chains`` (a graybox has no internal scan FFs)
    and one ``wrapper_chains`` entry in traced order, with a PASS ``latest_check``
    matching the netlist hash so ``_preflight_sim_scan`` accepts it. It is
    validated by the real ``check_scan`` in the caller/tests.
    """
    data = _load_json(soc_json_path)
    _, module = _top_module(data, top)

    si_net = _port_net(module, soc_wbr_si, "input")
    so_net = _port_net(module, soc_wbr_so, "output")
    _port_net(module, soc_wbr_se, "input")  # existence-check the scan-enable port
    clk_net = _port_net(module, clock_port, "input")

    ordered = trace_wrapper_chain(module, si_net)
    if not ordered:
        raise ScanError("no WBR scan cells found on the SoC wrapper chain")

    cells = module.get("cells", {})
    cell_records: list[dict[str, Any]] = []
    for pos, inst in enumerate(ordered):
        cell = cells[inst]
        cell_records.append(
            {
                "instance": inst,
                "original_type": str(cell.get("type", "")),
                "chain_index": 0,
                "chain_position": pos,
                "clock_net": _one_bit(cell, "CLK", inst),
                "data_net": _one_bit(
                    cell,
                    (
                        "FROM_SYS"
                        if cell.get("type") in _WBR_SCAN_IN_TYPES
                        else "FROM_CORE"
                    ),
                    inst,
                ),
                "scan_in_net": _one_bit(cell, "CTI", inst),
                "scan_enable_net": _one_bit(cell, "SE", inst),
                "q_net": _one_bit(cell, "CTO", inst),
            }
        )

    generic_hash = hash_file(soc_json_path)
    length = len(ordered)
    return {
        "version": 2,
        "top": top,
        "source_json": str(soc_json_path),
        "generic_json": str(soc_json_path),
        "generic_json_hash": generic_hash,
        "techmap_verilog": techmap_verilog,
        "sky130_verilog": None,
        "chain_count": 1,
        "cell_count": length,
        "clock_nets": [clk_net],
        "scan_inputs": [soc_wbr_si],
        "scan_outputs": [soc_wbr_so],
        "scan_enable": soc_wbr_se,
        "max_chain_length": length,
        "min_chain_length": length,
        "chain_order_policy": "traced from soc scan-in (CTI->CTO)",
        "chains": [],
        "wrapper_chains": [
            {
                "index": 0,
                "scan_in": soc_wbr_si,
                "scan_out": soc_wbr_so,
                "scan_in_net": si_net,
                "scan_out_net": so_net,
                "length": length,
                "cells": list(ordered),
                "cell_records": cell_records,
            }
        ],
        # Top-level `cells` is the flat list of INTERNAL scan FFs (consumed by
        # build_scan_atpg_view). A graybox has none -- the WBR cells live only in
        # wrapper_chains, fused downstream. Keep it empty.
        "cells": [],
        "ineligible_ffs": [],
        "latest_check": {
            "timestamp": utc_timestamp(),
            "status": "PASS",
            "warnings": [],
            "errors": [],
            "normal_mode": {"vector_count": 0},
            "generic_json_hash": generic_hash,
        },
    }
