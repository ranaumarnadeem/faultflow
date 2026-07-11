"""check_scan_structure must accept a wrapper-only (graybox) manifest.

A hierarchical SoC EXTEST graybox contains ONLY IEEE-1500 WBR scan cells
(``$wbc_*_scan_faultflow``) daisy-chained CTI->CTO, plus interconnect glue --
no internal ``$scanff_*`` FFs at all. The structural check historically only
knew about internal scan FFs: it collected the scan clock from them and
validated the internal ``chains`` list, ignoring WBR cells entirely. So a
graybox failed with "scanned design has no scan clock net" even though the WBR
cells carry a perfectly good CLK. The check must instead ALSO recognise the
wrapper chain -- collect its clock, verify its cells share the scan-enable, and
validate its CTI->CTO connectivity against the manifest ``wrapper_chains`` (the
same guarantee it already gives internal chains via SDI->Q).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from faultflow.scan.checks import check_scan_structure


def _graybox(tmp: Path) -> dict[str, Any]:
    """A minimal EXTEST graybox: soc_wbr_si -> wbc_in -> (glue INV) -> wbc_out
    -> soc_wbr_so, with shared clk(2) + scan-enable(3). No internal scan FFs."""
    return {
        "creator": "test",
        "modules": {
            "soc_top": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "soc_wbr_se": {"direction": "input", "bits": [3]},
                    "soc_wbr_si": {"direction": "input", "bits": [100]},
                    "soc_wbr_so": {"direction": "output", "bits": [102]},
                    "IN": {"direction": "input", "bits": [10]},
                    "OUT": {"direction": "output", "bits": [11]},
                },
                "cells": {
                    "u_a__wi": {
                        "hide_name": 0,
                        "type": "$wbc_in_scan_faultflow",
                        "parameters": {},
                        "attributes": {
                            "faultflow_wbr": "input",
                            "faultflow_wbr_chain": "0",
                            "wbr_bit": "0",
                            "faultflow_block": "blkA",
                            "faultflow_wbc": "__wi",
                        },
                        "port_directions": {
                            "CLK": "input",
                            "FROM_SYS": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_CORE": "output",
                            "CTO": "output",
                        },
                        "connections": {
                            "CLK": [2],
                            "FROM_SYS": [10],
                            "CTI": [100],
                            "SE": [3],
                            "TO_CORE": [20],
                            "CTO": [101],
                        },
                    },
                    "g_ic": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [20], "Y": [21]},
                    },
                    "u_b__wo": {
                        "hide_name": 0,
                        "type": "$wbc_out_scan_faultflow",
                        "parameters": {},
                        "attributes": {
                            "faultflow_wbr": "output",
                            "faultflow_wbr_chain": "0",
                            "wbr_bit": "1",
                            "faultflow_block": "blkB",
                            "faultflow_wbc": "__wo",
                        },
                        "port_directions": {
                            "CLK": "input",
                            "FROM_CORE": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_SYS": "output",
                            "CTO": "output",
                        },
                        "connections": {
                            "CLK": [2],
                            "FROM_CORE": [21],
                            "CTI": [101],
                            "SE": [3],
                            "TO_SYS": [11],
                            "CTO": [102],
                        },
                    },
                },
                "netnames": {},
            }
        },
    }


def _write(tmp: Path, data: dict[str, Any]) -> tuple[Path, str]:
    p = tmp / "graybox.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return p, h


def _manifest(gpath: Path, ghash: str, cells: list[str]) -> dict[str, Any]:
    return {
        "version": 2,
        "top": "soc_top",
        "generic_json": str(gpath),
        "generic_json_hash": ghash,
        "chain_count": 1,
        "cell_count": len(cells),
        "clock_nets": [2],
        "scan_inputs": ["soc_wbr_si"],
        "scan_outputs": ["soc_wbr_so"],
        "scan_enable": "soc_wbr_se",
        "max_chain_length": len(cells),
        "chains": [],
        "wrapper_chains": [
            {
                "index": 0,
                "scan_in": "soc_wbr_si",
                "scan_out": "soc_wbr_so",
                "scan_in_net": 100,
                "scan_out_net": 102,
                "length": len(cells),
                "cells": cells,
            }
        ],
        "cells": [],
        "ineligible_ffs": [],
    }


def test_wrapper_only_manifest_passes(tmp_path: Path) -> None:
    gpath, ghash = _write(tmp_path, _graybox(tmp_path))
    manifest = _manifest(gpath, ghash, ["u_a__wi", "u_b__wo"])
    result = check_scan_structure(manifest)
    assert result.passed, result.errors


def test_wrapper_chain_wrong_order_fails(tmp_path: Path) -> None:
    gpath, ghash = _write(tmp_path, _graybox(tmp_path))
    # Reversed cell order does not match the CTI->CTO connectivity.
    manifest = _manifest(gpath, ghash, ["u_b__wo", "u_a__wi"])
    result = check_scan_structure(manifest)
    assert not result.passed
    assert any(
        "connectivity" in e or "order" in e for e in result.errors
    ), result.errors


def test_wrapper_cell_bad_scan_enable_fails(tmp_path: Path) -> None:
    data = _graybox(tmp_path)
    # Point one WBR cell's SE at a different net than the shared scan-enable.
    data["modules"]["soc_top"]["cells"]["u_a__wi"]["connections"]["SE"] = [999]
    gpath, ghash = _write(tmp_path, data)
    manifest = _manifest(gpath, ghash, ["u_a__wi", "u_b__wo"])
    result = check_scan_structure(manifest)
    assert not result.passed
    assert any("scan enable" in e for e in result.errors), result.errors
