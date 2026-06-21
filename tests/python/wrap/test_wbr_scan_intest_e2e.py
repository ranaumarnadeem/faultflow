"""End-to-end INTEST scan-fusion coverage through the `sim --scan` CLI.

This is the integration counterpart of test_wbr_scan_intest.py (which unit-tests
the fusion mechanism). Here the whole `_sim_scan` runner hook is exercised on a
tiny core that is BOTH scan-inserted AND IEEE 1500 wrapped, proving:

  * the fused INTEST view drives the core through `__wbi_*` (wrapper boundary)
    and the scan FF state through `__ppi_*`, and observes core outputs through
    `__wbo_*` plus scan-captured internals through `__ppo_*`;
  * a fault that sits *behind* the scan FF (in its D-cone) -- undetectable by the
    combinational INTEST path that seeds FFs to 0 -- is DETECTED via scan
    capture+unload, yielding real (> 0%) coverage;
  * the system-side generic nets dropped by the fusion are tagged
    `wbr_decoupled` (they leave the coverage denominator) rather than raising
    `boundary_map_missing`;
  * the golden-protocol gate passes -- the name-mapping makes the generic
    FUNCTIONAL scan protocol agree with the fused reduced view, so the run does
    not abort with `golden_sequence_failed`.

Fixture `tiny_wrapped_dff` (net ids in []):

    D[3] --wbc_in __wi_D--> __core_D[8] --INV g0--> n_d[9] --> FF u0.D
    FF u0.Q[10] --INV g1--> core_q[11] --wbc_out __wo_Q--> Q[6]

`n_d` (net 9) is the behind-FF D-cone net: combinationally it only feeds the FF
D pin (a dead end), so a stuck-at on it is observable only after a scan capture
shifts the FF state out.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.db import connect, summary
from faultflow.runner import Runner
from faultflow.scan.reports import hash_file, utc_timestamp

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "tiny_wrapped_dff"


def _generic_wrapped_scan_json() -> dict[str, Any]:
    """A core that is both scan-inserted (`$scanff_faultflow`) and IEEE 1500
    wrapped (`$wbc_*`). This is the *generic* netlist the golden gate / fault
    grading run against; the runner fuses it into the reduced view internally."""
    return {
        "creator": "test fixture",
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "scan_in": {"direction": "input", "bits": [4]},
                    "scan_en": {"direction": "input", "bits": [5]},
                    "Q": {"direction": "output", "bits": [6]},
                    "scan_out": {"direction": "output", "bits": [10]},
                },
                "cells": {
                    "__wi_D": {
                        "hide_name": 0,
                        "type": "$wbc_in_faultflow",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "FROM_SYS": "input",
                            "TO_CORE": "output",
                        },
                        "connections": {"FROM_SYS": [3], "TO_CORE": [8]},
                    },
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [8], "Y": [9]},
                    },
                    "u0": {
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
                            "CLK": [2],
                            "D": [9],
                            "SDI": [4],
                            "SE": [5],
                            "Q": [10],
                        },
                    },
                    "g1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [10], "Y": [11]},
                    },
                    "__wo_Q": {
                        "hide_name": 0,
                        "type": "$wbc_out_faultflow",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "FROM_CORE": "input",
                            "TO_SYS": "output",
                        },
                        "connections": {"FROM_CORE": [11], "TO_SYS": [6]},
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "scan_in": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "scan_en": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "__core_D": {"hide_name": 0, "bits": [8], "attributes": {}},
                    "n_d": {"hide_name": 0, "bits": [9], "attributes": {}},
                    "q": {"hide_name": 0, "bits": [10], "attributes": {}},
                    "core_q": {"hide_name": 0, "bits": [11], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "scan_out": {"hide_name": 0, "bits": [10], "attributes": {}},
                },
            }
        },
    }


def _manifest(generic_path: Path) -> dict[str, Any]:
    return {
        "version": 2,
        "top": TOP,
        "generic_json": str(generic_path),
        "generic_json_hash": hash_file(generic_path),
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
                "scan_out_net": 10,
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
                "data_net": 9,
                "scan_in_net": 4,
                "scan_enable_net": 5,
                "q_net": 10,
            }
        ],
        "ineligible_ffs": [],
    }


def _write_config(path: Path, netlist: Path, mode: str = "intest") -> Path:
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
compaction = none
max_rounds = 4

[testmode]
mode = {mode}
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


def _install_wrapped_scan_workspace(tmp_path: Path, mode: str = "intest") -> Runner:
    source = tmp_path / f"{TOP}.json"
    source.write_text(
        json.dumps(_generic_wrapped_scan_json(), indent=2) + "\n", "utf-8"
    )
    cfg_path = _write_config(tmp_path / "config.ofs", source, mode=mode)
    cfg = load_config(cfg_path, TOP)
    # Keep the process CWD at the repo root (so the relative coverage schema path
    # resolves) but route all workspace output under tmp_path.
    cfg = replace(cfg, output_root=tmp_path / "out")
    cfg.ensure_workspace()

    # The generic scanned JSON the manifest points at IS our hand-built fixture.
    generic = cfg.scan_json_path
    generic.write_text(
        json.dumps(_generic_wrapped_scan_json(), indent=2) + "\n", "utf-8"
    )
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test techmap\n", encoding="utf-8")

    generic_hash = hash_file(generic)
    manifest = _manifest(generic)
    manifest["techmap_verilog"] = str(techmap)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": generic_hash,
    }
    cfg.scan_manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return Runner(cfg)


def _scan_campaign_id(db_path: Path) -> int:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE campaign_type = 'scan' ORDER BY id LIMIT 1"
        ).fetchone()
    assert row is not None, "no scan campaign created"
    return int(row[0])


@pytest.mark.golden
def test_intest_scan_fusion_yields_real_coverage(
    tmp_path: Path, require_cpp_core: None
) -> None:
    runner = _install_wrapped_scan_workspace(tmp_path)

    # The real S5 hook runs here: fuse_wbr_into_view + build_wbr_generic_name_map
    # + the scan ATPG pipeline on the fused view. A golden-gate failure would
    # raise RunnerError("golden_sequence_failed: ..."); a denominator landmine
    # would raise RunnerError("boundary_map_missing: ...").
    result = runner.sim(scan=True)
    assert "mode=scan" in result

    db_path = runner.cfg.db_path
    campaign_id = _scan_campaign_id(db_path)

    # (1) Real coverage -- the behind-FF cone is reachable only via scan unload.
    with connect(db_path) as conn:
        cov = summary(conn, campaign_id=campaign_id)
    assert cov["coverage_percent"] > 0.0

    with connect(db_path) as conn:
        # (2) The behind-FF D-cone fault (net 9 = n_d) is detected.
        detected_nd = conn.execute(
            """
            SELECT COUNT(*) FROM faults
            WHERE campaign_id = ?
              AND fault_site_key LIKE 'net:9:%'
              AND status = 'detected'
            """,
            (campaign_id,),
        ).fetchone()[0]
        assert detected_nd > 0, "behind-FF D-cone fault was not detected via scan"

        # (3) System-side nets (D=net 3 FROM_SYS, Q=net 6 TO_SYS) are decoupled,
        #     not mapped -- so no boundary_map_missing and they leave the
        #     denominator.
        decoupled = conn.execute(
            """
            SELECT DISTINCT
              CAST(substr(fault_site_key, 5, instr(substr(fault_site_key, 5), ':') - 1)
                   AS INTEGER) AS yid
            FROM faults
            WHERE campaign_id = ? AND exclusion = 'wbr_decoupled'
            """,
            (campaign_id,),
        ).fetchall()
        decoupled_yids = {int(r[0]) for r in decoupled}
        assert 3 in decoupled_yids  # D (system PI side of wbc_in)
        assert 6 in decoupled_yids  # Q (system PO side of wbc_out)

        # No active site should be left without a mapping AND without exclusion
        # (that is exactly the boundary_map_missing condition; if it were hit the
        # run would already have raised, but assert the post-state too).
        orphan = conn.execute(
            """
            SELECT COUNT(*) FROM faults
            WHERE campaign_id = ?
              AND exclusion = 'none'
              AND collapsed_into IS NULL
              AND atpg_compiled_net_index IS NULL
            """,
            (campaign_id,),
        ).fetchone()[0]
        assert orphan == 0


@pytest.mark.golden
def test_extest_scan_yields_interconnect_coverage(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """EXTEST fuses to a combinational boundary view (core safed, __ppo_ dropped)
    and runs plain native ATPG on it. The wrapper boundary nets (D's FROM_SYS =
    net 3, Q's TO_SYS = net 6) are controllable+observable interconnect faults
    and must be detected; the dead-core nets behind the const-0 safe (e.g. the
    D-cone net 9) are unobservable here and must be redundant/excluded, leaving
    the denominator so coverage reflects the testable boundary."""
    runner = _install_wrapped_scan_workspace(tmp_path, mode="extest")

    # Routes through Runner.sim -> _sim_extest (combinational ATPG on fused view).
    result = runner.sim(scan=True)
    assert "mode=extest" in result

    db_path = runner.cfg.db_path
    campaign_id = _scan_campaign_id(db_path)

    with connect(db_path) as conn:
        cov = summary(conn, campaign_id=campaign_id)
        # Real interconnect coverage (boundary faults detected, dead core out).
        assert cov["coverage_percent"] > 0.0

        # A wrapper-boundary fault (net 3 = D/FROM_SYS or net 6 = Q/TO_SYS) is
        # detected -- these are exactly what EXTEST is meant to cover.
        boundary_detected = conn.execute(
            """
            SELECT COUNT(*) FROM faults
            WHERE campaign_id = ?
              AND status = 'detected'
              AND (fault_site_key LIKE 'net:3:%' OR fault_site_key LIKE 'net:6:%')
            """,
            (campaign_id,),
        ).fetchone()[0]
        assert boundary_detected > 0, "no wrapper-boundary fault detected in EXTEST"

        # The behind-safe core net 9 is unobservable in the EXTEST view, so it
        # must NOT count toward the denominator (redundant or excluded).
        core_in_denom = conn.execute(
            """
            SELECT COUNT(*) FROM faults
            WHERE campaign_id = ?
              AND fault_site_key LIKE 'net:9:%'
              AND exclusion = 'none'
              AND collapsed_into IS NULL
              AND status != 'redundant'
            """,
            (campaign_id,),
        ).fetchone()[0]
        assert core_in_denom == 0, "dead-core fault wrongly left in EXTEST denominator"
