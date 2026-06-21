"""Synthetic 2-block SoC fixture (`soc2`) for hierarchical aggregation tests.

Two tiny wrapped + scan-inserted blocks (each = core + WBC ring + one scan FF,
the proven `tiny_wrapped_dff` shape) plus a combinational assembly netlist whose
block *cores* are blackboxed, used for the interconnect EXTEST.

    block <top>:  D -wbc_in-> __core_D -INV g0-> n_d -> FF u0.D
                  FF u0.Q -INV g1-> core_q -wbc_out-> Q   (+ scan_in/scan_en/scan_out)

    assembly soc2_top:  u_coreA(blackbox).Q -wbc_out-> icA -INV g_ic-> icB
                        -wbc_in-> coreB_d -> u_coreB(blackbox).D
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def block_json(top: str) -> dict[str, Any]:
    """A scan-inserted + IEEE-1500 scan-WBC wrapped standalone block top.

    Net map:
      2=CLK  3=D  4=scan_in  5=scan_en  6=Q  7=wbr_si  8=wbr_se
      9=__core_D  10=n_d  11=q(=scan_out)  12=core_q  13=wbr_chain  14=wbr_so
    Wrapper chain: wbr_si(7) -> __wi_D[CTI->CTO=13] -> __wo_Q[CTI->CTO=14] -> wbr_so
    """
    return {
        "creator": "soc2 fixture",
        "modules": {
            top: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "scan_in": {"direction": "input", "bits": [4]},
                    "scan_en": {"direction": "input", "bits": [5]},
                    "Q": {"direction": "output", "bits": [6]},
                    "scan_out": {"direction": "output", "bits": [11]},
                    "wbr_si": {"direction": "input", "bits": [7]},
                    "wbr_se": {"direction": "input", "bits": [8]},
                    "wbr_so": {"direction": "output", "bits": [14]},
                },
                "cells": {
                    "__wi_D": _scan_wbc(
                        "$wbc_in_scan_faultflow",
                        {
                            "CLK": [2],
                            "FROM_SYS": [3],
                            "CTI": [7],
                            "SE": [8],
                            "TO_CORE": [9],
                            "CTO": [13],
                        },
                        {
                            "CLK": "input",
                            "FROM_SYS": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_CORE": "output",
                            "CTO": "output",
                        },
                        wbr="in",
                        chain=0,
                        bit=0,
                    ),
                    "g0": _cell(
                        "sky130_fd_sc_hd__inv_1",
                        {"A": [9], "Y": [10]},
                        {"A": "input", "Y": "output"},
                    ),
                    "u0": _cell(
                        "$scanff_faultflow",
                        {"CLK": [2], "D": [10], "SDI": [4], "SE": [5], "Q": [11]},
                        {
                            "CLK": "input",
                            "D": "input",
                            "SDI": "input",
                            "SE": "input",
                            "Q": "output",
                        },
                    ),
                    "g1": _cell(
                        "sky130_fd_sc_hd__inv_1",
                        {"A": [11], "Y": [12]},
                        {"A": "input", "Y": "output"},
                    ),
                    "__wo_Q": _scan_wbc(
                        "$wbc_out_scan_faultflow",
                        {
                            "CLK": [2],
                            "FROM_CORE": [12],
                            "CTI": [13],
                            "SE": [8],
                            "TO_SYS": [6],
                            "CTO": [14],
                        },
                        {
                            "CLK": "input",
                            "FROM_CORE": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_SYS": "output",
                            "CTO": "output",
                        },
                        wbr="out",
                        chain=0,
                        bit=1,
                    ),
                },
                "netnames": {
                    "CLK": _net(2),
                    "D": _net(3),
                    "scan_in": _net(4),
                    "scan_en": _net(5),
                    "Q": _net(6),
                    "wbr_si": _net(7),
                    "wbr_se": _net(8),
                    "__core_D": _net(9),
                    "n_d": _net(10),
                    "q": _net(11),
                    "core_q": _net(12),
                    "__wbr_chain": _net(13),
                    "wbr_so": _net(14),
                    "scan_out": _net(11),
                },
            }
        },
    }


def block_manifest(top: str) -> dict[str, Any]:
    """Pre-authored manifest for the scan-WBC block fixture.

    Net map mirrors block_json:
      2=CLK  3=D  4=scan_in  5=scan_en  6=Q  7=wbr_si  8=wbr_se
      9=__core_D  10=n_d  11=q(scan_out)  12=core_q  13=__wbr_chain  14=wbr_so
    """
    return {
        "version": 2,
        "top": top,
        "clock_net": 2,
        "clock_nets": [2],
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in"],
        "scan_outputs": ["scan_out"],
        "max_chain_length": 1,
        "chains": [
            {
                "index": 0,
                "scan_in": "scan_in",
                "scan_out": "scan_out",
                "scan_in_net": 4,
                "scan_out_net": 11,
                "length": 1,
                "cells": ["u0"],
            }
        ],
        "cells": [
            {
                "instance": "u0",
                "original_type": "sky130_fd_sc_hd__dfxtp_1",
                "chain_index": 0,
                "chain_position": 0,
                "clock_net": 2,
                "data_net": 10,
                "scan_in_net": 4,
                "scan_enable_net": 5,
                "q_net": 11,
            }
        ],
        "wrapper_chains": [
            {
                "index": 0,
                "scan_in": "wbr_si",
                "scan_out": "wbr_so",
                "scan_in_net": 7,
                "scan_out_net": 14,
                "length": 2,
                "cells": ["__wi_D", "__wo_Q"],
                "cell_records": [
                    {
                        "instance": "__wi_D",
                        "original_type": "$wbc_in_scan_faultflow",
                        "chain_index": 0,
                        "chain_position": 0,
                        "clock_net": 2,
                        "data_net": 3,
                        "scan_in_net": 7,
                        "scan_enable_net": 8,
                        "q_net": 13,
                    },
                    {
                        "instance": "__wo_Q",
                        "original_type": "$wbc_out_scan_faultflow",
                        "chain_index": 0,
                        "chain_position": 1,
                        "clock_net": 2,
                        "data_net": 12,
                        "scan_in_net": 13,
                        "scan_enable_net": 8,
                        "q_net": 14,
                    },
                ],
            }
        ],
        "ineligible_ffs": [
            {
                "instance": "__wi_D",
                "cell_type": "$wbc_in_scan_faultflow",
                "reason": "wbr_scan_cell",
            },
            {
                "instance": "__wo_Q",
                "cell_type": "$wbc_out_scan_faultflow",
                "reason": "wbr_scan_cell",
            },
        ],
    }


def assembly_json(top: str = "soc2_top") -> dict[str, Any]:
    """Combinational assembly: blackboxed cores wrapped by the SAME boundary the
    blocks were tested with (every block boundary port wrapped, tagged to its block
    via faultflow_block/faultflow_wbc). The interconnect is a CLOSED LOOP so every
    wrapper-outward is interconnect-facing (no chip-boundary PI/PO wrappers, which
    interconnect EXTEST cannot test) and is therefore OWNED by the assembly:

        coreA.Q -__wo_A_Q-> ic1 -INV g_ic1-> ic2 -__wi_B_D-> coreB.D
        coreB.Q -__wo_B_Q-> ic3 -INV g_ic2-> ic4 -__wi_A_D-> coreA.D

    The blackboxed core outputs are pseudo-PIs (controllable), so the "loop" is not
    a real cycle — it is two independent interconnect paths.
    """
    return {
        "creator": "soc2 fixture",
        "modules": {
            top: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {},
                "cells": {
                    "__wi_A_D": _wbc(
                        "$wbc_in_faultflow",
                        {"FROM_SYS": [9], "TO_CORE": [2]},
                        {"FROM_SYS": "input", "TO_CORE": "output"},
                        block="blkA",
                        wbc="__wi_D",
                    ),
                    "u_coreA": _cell(
                        "$soc_core_faultflow",
                        {"D": [2], "Q": [3]},
                        {"D": "input", "Q": "output"},
                    ),
                    "__wo_A_Q": _wbc(
                        "$wbc_out_faultflow",
                        {"FROM_CORE": [3], "TO_SYS": [4]},
                        {"FROM_CORE": "input", "TO_SYS": "output"},
                        block="blkA",
                        wbc="__wo_Q",
                    ),
                    "g_ic1": _cell(
                        "sky130_fd_sc_hd__inv_1",
                        {"A": [4], "Y": [5]},
                        {"A": "input", "Y": "output"},
                    ),
                    "__wi_B_D": _wbc(
                        "$wbc_in_faultflow",
                        {"FROM_SYS": [5], "TO_CORE": [6]},
                        {"FROM_SYS": "input", "TO_CORE": "output"},
                        block="blkB",
                        wbc="__wi_D",
                    ),
                    "u_coreB": _cell(
                        "$soc_core_faultflow",
                        {"D": [6], "Q": [7]},
                        {"D": "input", "Q": "output"},
                    ),
                    "__wo_B_Q": _wbc(
                        "$wbc_out_faultflow",
                        {"FROM_CORE": [7], "TO_SYS": [8]},
                        {"FROM_CORE": "input", "TO_SYS": "output"},
                        block="blkB",
                        wbc="__wo_Q",
                    ),
                    "g_ic2": _cell(
                        "sky130_fd_sc_hd__inv_1",
                        {"A": [8], "Y": [9]},
                        {"A": "input", "Y": "output"},
                    ),
                },
                "netnames": {
                    "coreA_d": _net(2),
                    "coreA_q": _net(3),
                    "ic1": _net(4),
                    "ic2": _net(5),
                    "coreB_d": _net(6),
                    "coreB_q": _net(7),
                    "ic3": _net(8),
                    "ic4": _net(9),
                },
            }
        },
    }


def write_soc2(root: Path) -> Path:
    """Write the soc2 fixture tree + project manifest under `root`; return manifest."""
    soc = root / "soc2"
    soc.mkdir(parents=True, exist_ok=True)
    for name in ("blkA", "blkB"):
        (soc / f"{name}_scan.json").write_text(
            json.dumps(block_json(name), indent=2) + "\n", encoding="utf-8"
        )
        (soc / f"{name}_manifest.json").write_text(
            json.dumps(block_manifest(name), indent=2) + "\n", encoding="utf-8"
        )
    (soc / "soc2_top.json").write_text(
        json.dumps(assembly_json(), indent=2) + "\n", encoding="utf-8"
    )

    cell_map = Path(__file__).resolve().parents[2] / "cells/sky130/sky130_fd_sc_hd.json"
    (root / "base.ofs").write_text(
        f"""
[design]
netlist = {soc / 'blkA_scan.json'}
cell_lib = {cell_map}

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = blackbox

[atpg]
mode = comb
compaction = none
max_rounds = 4
""".strip() + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema": "faultflow_project_v1",
        "name": "soc2",
        "cell_map_profile": "sky130",
        "base_config": str(root / "base.ofs"),
        "blocks": [
            {
                "name": "blkA",
                "top": "blkA",
                "generic_json": str(soc / "blkA_scan.json"),
                "scan_manifest": str(soc / "blkA_manifest.json"),
            },
            {
                "name": "blkB",
                "top": "blkB",
                "generic_json": str(soc / "blkB_scan.json"),
                "scan_manifest": str(soc / "blkB_manifest.json"),
            },
        ],
        "interconnect": {
            "assembly_top": "soc2_top",
            "assembly_netlist": str(soc / "soc2_top.json"),
            "blackbox_instances": ["u_coreA", "u_coreB"],
        },
        "aggregation": {"policy": "disjoint_union"},
    }
    manifest_path = root / "project_soc2.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _cell(ctype: str, conns: dict[str, list[int]], dirs: dict[str, str]) -> dict:
    return {
        "hide_name": 0,
        "type": ctype,
        "parameters": {},
        "attributes": {},
        "port_directions": dirs,
        "connections": conns,
    }


def _wbc(
    ctype: str,
    conns: dict[str, list[int]],
    dirs: dict[str, str],
    *,
    block: str,
    wbc: str,
) -> dict:
    """A boundary cell tagged with the block + block-WBC instance it represents,
    so the aggregator can tie this assembly wrapper to the block it was tested in."""
    cell = _cell(ctype, conns, dirs)
    cell["attributes"] = {"faultflow_block": block, "faultflow_wbc": wbc}
    return cell


def _scan_wbc(
    ctype: str,
    conns: dict[str, list[int]],
    dirs: dict[str, str],
    *,
    wbr: str,
    chain: int,
    bit: int,
) -> dict:
    """A shiftable IEEE-1500 scan WBC tagged for stitch + aggregate identification."""
    cell = _cell(ctype, conns, dirs)
    cell["attributes"] = {
        "faultflow_wbr": wbr,
        "faultflow_wbr_chain": str(chain),
        "wbr_bit": str(bit),
    }
    return cell


def _net(bit: int) -> dict:
    return {"hide_name": 0, "bits": [bit], "attributes": {}}
