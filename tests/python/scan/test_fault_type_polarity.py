"""Regression for the SA0/SA1 case-mismatch bug in `_process_scan_candidate`.

Background: the fault-type code passed into the C++ tentative simulators was
computed with `1 if row.fault_type == "SA1" else 0` (and, symmetrically, the
Q-stem chain-integrity credit with `row.fault_type == "SA0"`), but the DB
stores lowercase `"sa0"`/`"sa1"` (see `sqlite_store.cpp`'s
`fault_type_to_string`). The uppercase comparison never matches, so every
fault was graded as if it were SA0 regardless of its true polarity.

This test isolates the bug on a single non-Q-stem net (the inverter output Y
in a tiny DFF->INV circuit), where the reduced-view tentative grade
(`_preloaded`, built at the buggy line) is the ONLY path that can credit a
detection -- there is no Q-stem chain-integrity fallback for this site.

With a vector that holds the loaded FF state (`__ppi_u0`) at 0, golden Y=1:
  * SA0-at-Y (forces Y=0) genuinely differs from golden -> correctly detected.
  * SA1-at-Y (forces Y=1) genuinely MATCHES golden -> must NOT be detected.

With the bug, the type code passed for BOTH faults is always 0 (SA0), so the
SA1 fault is graded as if it were SA0 too -- which this specific vector does
also detect -- producing a false positive: SA1 gets marked `detected` despite
never having been distinguished from golden.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.db import connect, init_schema
from faultflow.db.campaign import ensure_campaign
from faultflow.runner.runner import _port_names
from faultflow.scan import stitch_scan_json
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.detection_pipeline import (
    _FaultRow,
    _process_scan_candidate,
    build_scan_pipeline_context,
)
from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp
from faultflow.scan.site_resolution import build_site_key_index

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _tiny_dff_inv_json() -> dict[str, object]:
    # D -> DFF(u0) -> Q(net 4) -> INV(u_inv) -> Y(net 5). Y has no fanout beyond
    # the module PO, so it is a single fanout-free stem: exactly one fault site,
    # not a Q-stem net (Q itself, net 4, is the Q-stem; Y is ordinary logic).
    return {
        "modules": {
            "tiny_dff_inv": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Y": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "Q": [4]},
                    },
                    "u_inv": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [4], "Y": [5]},
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "Y": {"hide_name": 0, "bits": [5], "attributes": {}},
                },
            }
        }
    }


def _stuck_at_scan_workspace(tmp_path: Path):
    source = tmp_path / "tiny_dff_inv.json"
    source.write_text(
        json.dumps(_tiny_dff_inv_json(), indent=2) + "\n", encoding="utf-8"
    )
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {source}
cell_lib = {CELL_MAP}
[fault_model]
model = stuck_at
collapsing = false
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
random_vectors = 0
max_rounds = 3
[wrap]
wbr_model = buffer
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, "tiny_dff_inv")
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff_inv", generic)
    manifest = manifest_from_result(result, source, techmap, None)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": hash_file(generic),
    }
    cfg.scan_manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    view, pseudo_port_map = build_scan_atpg_view(
        json.loads(generic.read_text(encoding="utf-8")), manifest
    )
    atpg_view = cfg.intermediate_dir / "scan_atpg_view.json"
    atpg_view.write_text(json.dumps(view, indent=2) + "\n", encoding="utf-8")

    functional_outputs = [
        port
        for port in _port_names(atpg_view, cfg.top, "output")
        if not port.startswith("__ppo_")
    ]
    scan_ctx = build_scan_pipeline_context(
        cfg, manifest, generic, pseudo_port_map, functional_outputs
    )
    return cfg, atpg_view, generic, scan_ctx


@pytest.mark.golden
def test_non_qstem_sa1_fault_not_falsely_credited_by_sa0_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, generic, scan_ctx = _stuck_at_scan_workspace(tmp_path)

    import faultflow.runner.runner as runner_mod

    core = runner_mod._load_core()
    assert core is not None

    scan_cell_map = str(resolve_scan_cell_map(cfg))
    input_order = _port_names(atpg_view, cfg.top, "input")
    assert set(input_order) == {"D", "__ppi_u0"}

    generic_site_index = build_site_key_index(core, generic, scan_cell_map, "fail")

    fp = {
        "top": cfg.top,
        "netlist_hash": "polarity-test",
        "cell_lib_hash": "cell-test",
        "config_hash": "cfg-test",
        "template_hash": "tmpl-test",
        "yosys_version": "yosys",
        "faultflow_version": "test",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
        "fault_model": "stuck_at",
        "manifest_hash": str(scan_ctx.manifest.get("generic_json_hash", "")),
        "atpg_view_schema_ver": "scan-atpg-view-observe-buf-1",
    }

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)
        run_id = conn.execute(
            """
            INSERT INTO runs(campaign_id, status, vector_source, vector_count)
            VALUES (?, 'running', 'test', 0)
            """,
            (campaign_id,),
        ).lastrowid
        assert run_id is not None

        # Y (net 5) is a plain logic net -- not a Q-stem site (net:4:stem is).
        site_key = "net:5:stem"
        sa0_id = conn.execute(
            """
            INSERT INTO faults(
              campaign_id, fault_site_key, net_id, net_name, compiled_net_index,
              atpg_compiled_net_index, type, fault_type, status
            ) VALUES (?, ?, 5, 'Y', 1, 1, 'sa0', 'sa0', 'undetected')
            """,
            (campaign_id, site_key),
        ).lastrowid
        assert sa0_id is not None
        sa1_id = conn.execute(
            """
            INSERT INTO faults(
              campaign_id, fault_site_key, net_id, net_name, compiled_net_index,
              atpg_compiled_net_index, type, fault_type, status
            ) VALUES (?, ?, 5, 'Y', 1, 1, 'sa1', 'sa1', 'undetected')
            """,
            (campaign_id, site_key),
        ).lastrowid
        assert sa1_id is not None
        conn.commit()

        active_rows = [
            _FaultRow(
                fault_id=int(sa0_id),
                fault_site_key=site_key,
                fault_type="sa0",
                net_index=1,
                net_id=5,
            ),
            _FaultRow(
                fault_id=int(sa1_id),
                fault_site_key=site_key,
                fault_type="sa1",
                net_index=1,
                net_id=5,
            ),
        ]

        # Hold the loaded FF state (__ppi_u0, i.e. Q) at 0 -> golden Y = ~0 = 1.
        # SA0-at-Y (forces 0) differs from golden 1 -> genuinely detected.
        # SA1-at-Y (forces 1) MATCHES golden 1 -> must NOT be detected here.
        vector = {"D": False, "__ppi_u0": False}

        accepted, _protocol_no_progress, _pattern = _process_scan_candidate(
            core,
            conn,
            scan_ctx=scan_ctx,
            reduced_json_path=str(atpg_view),
            reduced_cell_map=scan_cell_map,
            generic_cell_map=scan_cell_map,
            db_path=str(cfg.db_path),
            campaign_id=campaign_id,
            run_id=int(run_id),
            candidate_id=1,
            vector_index=0,
            vector=vector,
            input_order=input_order,
            source="random",
            sat_target_fault_id=None,
            active_rows=active_rows,
            generic_site_index=generic_site_index,
            unsupported="fail",
            transition=False,
        )
        assert accepted, "expected the SA0-detecting vector to be accepted"

        statuses = {
            row["id"]: str(row["status"])
            for row in conn.execute(
                "SELECT id, status FROM faults WHERE id IN (?, ?)",
                (sa0_id, sa1_id),
            ).fetchall()
        }

    assert (
        statuses[sa0_id] == "detected"
    ), "SA0-at-Y genuinely differs from golden Y=1 and must be detected"
    assert statuses[sa1_id] == "undetected", (
        "SA1-at-Y matches golden Y=1 on this vector and must NOT be marked "
        "detected -- a 'detected' status here is the SA0/SA1 case-mismatch "
        "bug (fault_type compared against 'SA1'/'SA0' instead of 'sa1'/'sa0')"
    )
