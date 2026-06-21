from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.db import connect, init_schema
from faultflow.db.campaign import ensure_campaign
from faultflow.runner.progressive_atpg import redundancy_model_id
from faultflow.scan import stitch_scan_json
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.detection_pipeline import (
    build_los_couples,
    build_scan_pipeline_context,
    run_progressive_scan_atpg,
)
from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _tiny_dff_inv_json() -> dict[str, object]:
    # A scan-able DFF whose Q drives an inverter to functional PO Y. Under LOS the
    # single FF is a chain head, so its launched Q transition comes from the fresh
    # scan-in bit; it propagates to Y.
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


def _los_scan_workspace(tmp_path: Path):
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
model = transition
launch = los
collapsing = false
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
random_vectors = 0
max_rounds = 3
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

    from faultflow.runner.runner import _port_names

    functional_outputs = [
        port
        for port in _port_names(atpg_view, cfg.top, "output")
        if not port.startswith("__ppo_")
    ]
    scan_ctx = build_scan_pipeline_context(
        cfg, manifest, generic, pseudo_port_map, functional_outputs
    )
    fp = {
        "top": cfg.top,
        "netlist_hash": "scan-los",
        "cell_lib_hash": "cell-test",
        "config_hash": "cfg-test",
        "template_hash": "tmpl-test",
        "yosys_version": "yosys",
        "faultflow_version": "test",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
        "fault_model": "transition",
        "launch": "los",
        "manifest_hash": str(manifest.get("generic_json_hash", "")),
        "atpg_view_schema_ver": "scan-atpg-view-observe-buf-1",
    }
    return cfg, atpg_view, scan_ctx, fp, pseudo_port_map


def test_build_los_couples_single_ff_chain(tmp_path: Path) -> None:
    _cfg, _view, _ctx, _fp, pseudo_port_map = _los_scan_workspace(tmp_path)
    couple_ports, head_ports, head_by_chain = build_los_couples(pseudo_port_map)
    # One FF -> a single chain head, no couples (no predecessor).
    assert couple_ports == []
    assert len(head_ports) == 1
    assert set(head_by_chain.values()) == set(head_ports)


@pytest.mark.integration
@pytest.mark.golden
def test_scan_los_run_accepts_and_persists_launch_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp, _ = _los_scan_workspace(tmp_path)
    assert cfg.fault_model.launch == "los"

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)

    _vectors, stats, _run_id, _atpg_s, _sim_s = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=3,
        target_coverage=100.0,
        transition=True,
        launch_mode="los",
    )

    assert stats.accepted_vectors >= 1

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT pattern, launch_pattern FROM vectors WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()

    assert rows
    assert any(
        str(row["launch_pattern"]) and set(str(row["launch_pattern"])) <= {"0", "1"}
        for row in rows
    )
