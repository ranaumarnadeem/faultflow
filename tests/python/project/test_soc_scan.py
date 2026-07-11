"""faultflow.project.soc_scan: trace + generate a SoC wrapper-chain manifest.

The generator's contract: produce a manifest for a composed EXTEST graybox
(wrapper cells daisy-chained through the glue, no internal scan FFs) that the
REAL check_scan accepts -- so we assert against check_scan_structure directly,
not a re-implementation of its rules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.checks import check_scan_structure
from faultflow.scan.errors import ScanError
from faultflow.project.soc_scan import build_soc_scan_manifest, trace_wrapper_chain


def _graybox() -> dict[str, Any]:
    """soc_wbr_si -> u_a__wi -> (glue INV) -> u_b__wo -> soc_wbr_so; shared clk/se.

    The daisy-chain link is CTO(u_a__wi)=net 101 == CTI(u_b__wo)=net 101.
    """
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
                        "attributes": {"faultflow_wbr_chain": "0", "wbr_bit": "0"},
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
                        "attributes": {"faultflow_wbr_chain": "0", "wbr_bit": "0"},
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


def _write(tmp: Path, data: dict[str, Any]) -> Path:
    p = tmp / "graybox.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def test_trace_follows_cti_cto_order() -> None:
    module = _graybox()["modules"]["soc_top"]
    # soc_wbr_si drives net 100 (u_a__wi.CTI). Chain: u_a__wi -> u_b__wo.
    assert trace_wrapper_chain(module, 100) == ["u_a__wi", "u_b__wo"]


def test_trace_rejects_undaisychained_cells() -> None:
    data = _graybox()
    # Break the daisy chain: u_b__wo.CTI no longer == u_a__wi.CTO.
    data["modules"]["soc_top"]["cells"]["u_b__wo"]["connections"]["CTI"] = [500]
    module = data["modules"]["soc_top"]
    with pytest.raises(ScanError, match="not reachable"):
        trace_wrapper_chain(module, 100)


def test_generated_manifest_passes_real_check_scan(tmp_path: Path) -> None:
    gpath = _write(tmp_path, _graybox())
    manifest = build_soc_scan_manifest(
        gpath,
        "soc_top",
        soc_wbr_si="soc_wbr_si",
        soc_wbr_so="soc_wbr_so",
        soc_wbr_se="soc_wbr_se",
        clock_port="clk",
    )
    # The strongest possible assertion: the REAL check_scan accepts it.
    result = check_scan_structure(manifest)
    assert result.passed, result.errors
    assert manifest["wrapper_chains"][0]["cells"] == ["u_a__wi", "u_b__wo"]
    assert manifest["chains"] == []
    assert manifest["latest_check"]["status"] == "PASS"
    # Hash in latest_check must match the on-disk generic JSON (preflight checks it).
    expected = hashlib.sha256(gpath.read_bytes()).hexdigest()
    assert manifest["generic_json_hash"] == expected
    assert manifest["latest_check"]["generic_json_hash"] == expected
