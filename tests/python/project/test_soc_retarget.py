"""Stage 5 retarget proof: a block's INTEST pattern, retargeted onto a SoC scan
chain (block segment + a BYPASS register), reproduces the block's unload when
replayed on the assembly netlist — with no re-ATPG at the top.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from faultflow.retarget.emit import (
    dict_to_pattern,
    pattern_to_dict,
    read_retargeted,
    write_retargeted,
)
from faultflow.retarget.soc_access import parse_soc_access
from faultflow.retarget.transform import retarget_block_pattern
from faultflow.retarget.verify_soc import verify_soc
from faultflow.scan.protocol import ScanPattern
from faultflow.wrap import wrap_ports

_REPO = Path(__file__).resolve().parents[2]
_CELL_MAP = _REPO / "cells" / "sky130" / "sky130_fd_sc_hd.json"


def _and_core() -> dict[str, Any]:
    return {
        "creator": "test",
        "modules": {
            "blkB": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "b": {"direction": "input", "bits": [3]},
                    "y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        "connections": {"A": [2], "B": [3], "X": [4]},
                    }
                },
                "netnames": {
                    "a": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "b": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "y": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        },
    }


def _build_soc(block: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Daisy the block's wrapper chain into a SoC chain with a trailing BYPASS scan
    FF: wbr_si -> [block wrapper cells] -> bypass -> soc_so. Returns (soc, max_net)."""
    soc = copy.deepcopy(block)
    mod = soc["modules"]["blkB"]
    ports = mod["ports"]
    cells = mod["cells"]
    netnames = mod["netnames"]

    used = {
        b
        for c in cells.values()
        for bits in c["connections"].values()
        for b in bits
        if isinstance(b, int)
    }
    used |= {b for p in ports.values() for b in p["bits"] if isinstance(b, int)}
    soc_so = max(used) + 1

    block_so_net = ports["wbr_so"]["bits"][0]  # __wo_y.CTO
    clk = ports["CLK"]["bits"][0]
    se = ports["wbr_se"]["bits"][0]
    del ports["wbr_so"]  # now internal: feeds the BYPASS register

    cells["bypass"] = {
        "hide_name": 0,
        "type": "$scanff_faultflow",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            "CLK": "input",
            "D": "input",
            "SDI": "input",
            "SE": "input",
            "Q": "output",
        },
        "connections": {
            "CLK": [clk],
            "D": [block_so_net],
            "SDI": [block_so_net],
            "SE": [se],
            "Q": [soc_so],
        },
    }
    ports["soc_so"] = {"direction": "output", "bits": [soc_so]}
    netnames["soc_so"] = {"hide_name": 0, "bits": [soc_so], "attributes": {}}
    return soc, soc_so


def _soc_access() -> Any:
    return parse_soc_access(
        {
            "schema": "faultflow_soc_access_v1",
            "assembly_top": "blkB",
            "soc_scan": {
                "scan_inputs": ["wbr_si"],
                "scan_outputs": ["soc_so"],
                "scan_enable": "wbr_se",
                "clock_ports": ["CLK"],
                "max_chain_length": 4,
            },
            "soc_chains": [
                {
                    "index": 0,
                    "scan_in": "wbr_si",
                    "scan_out": "soc_so",
                    "length": 4,
                    "segments": [
                        {
                            "block": "blkB",
                            "block_chain": 0,
                            "soc_offset": 0,
                            "length": 3,
                        },
                        {
                            "block": "__bypass",
                            "block_chain": None,
                            "soc_offset": 3,
                            "length": 1,
                        },
                    ],
                }
            ],
            "wrapper_instruction": {"blkB": "INTEST"},
        }
    )


def _block_pattern(core: Any, block_netlist: Path, qa: bool, qb: bool) -> ScanPattern:
    """Run the block's own INTEST scan to get its real per-chain load/unload."""
    load = [False, qb, qa]  # descending: __wo_y(dc), __wi_b=qb, __wi_a=qa
    res = core.simulate_scan_pattern(
        json_path=str(block_netlist),
        cell_map_path=str(_CELL_MAP),
        clock_ports=["CLK"],
        clock_off_states=[False],
        scan_enable_port="wbr_se",
        scan_input_ports=["wbr_si"],
        scan_output_ports=["wbr_so"],
        functional_output_ports=[],
        max_chain_length=3,
        load_seqs={0: load},
        capture_pi_values={"a": False, "b": False},
        unsupported_policy="blackbox",
        test_mode="intest",
    )
    return ScanPattern(
        load_seqs={0: load},
        capture_pi_values={"a": False, "b": False},
        expected_unload={0: list(res["unload_seqs"][0])},
    )


def test_retarget_block_pattern_verifies_on_soc(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None

    wrapped = wrap_ports(_and_core(), wbr_model="scan")
    block_netlist = tmp_path / "blkB_scan.json"
    block_netlist.write_text(json.dumps(wrapped), encoding="utf-8")

    soc, _ = _build_soc(wrapped)
    soc_netlist = tmp_path / "soc.json"
    soc_netlist.write_text(json.dumps(soc), encoding="utf-8")

    access = _soc_access()

    for qa in (False, True):
        for qb in (False, True):
            bp = _block_pattern(core, block_netlist, qa, qb)
            # Sanity: the block's own unload already shows y = qa & qb at the head.
            assert bp.expected_unload[0][0] == (qa and qb)

            soc_pat = retarget_block_pattern(bp, access, "blkB")
            res = verify_soc(soc_netlist, _CELL_MAP, access, soc_pat, core=core)
            assert res.verified, (qa, qb, res.mismatches)
            assert res.compared == 3  # blkB owns 3 of the 4 SoC positions

            # Emit -> reload -> re-verify (portable round-trip).
            payload = pattern_to_dict(soc_pat, assembly_top="blkB")
            out = write_retargeted(tmp_path / "retgt.json", payload)
            reloaded = dict_to_pattern(read_retargeted(out))
            res2 = verify_soc(soc_netlist, _CELL_MAP, access, reloaded, core=core)
            assert res2.verified, (qa, qb, res2.mismatches)


def test_retarget_rejects_unmapped_block_chain() -> None:
    from faultflow.retarget.soc_access import SocAccessError

    access = _soc_access()
    bp = ScanPattern(
        load_seqs={0: [False], 7: [True]},  # chain 7 has no SoC segment
        capture_pi_values={},
        expected_unload={0: [False], 7: [True]},
    )
    try:
        retarget_block_pattern(bp, access, "blkB")
    except SocAccessError as exc:
        assert "chains [7]" in str(exc) or "chain" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SocAccessError for unmapped block chain")
