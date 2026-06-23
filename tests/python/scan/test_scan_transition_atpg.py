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
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.detection_pipeline import (
    _detected_fault_rows,
    build_scan_pipeline_context,
    compact_run_scan_transition,
    run_progressive_scan_atpg,
)
from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _tiny_dff_inv_json() -> dict[str, object]:
    # A scan-able DFF whose Q (net 4) drives an inverter to functional PO Y, so a
    # launched Q transition propagates to an observable (unlike a bare D=PI flop).
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


def _transition_scan_workspace(tmp_path: Path):
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
launch = loc
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
        "netlist_hash": "scan-trans",
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
        "manifest_hash": str(manifest.get("generic_json_hash", "")),
        "atpg_view_schema_ver": "scan-atpg-view-observe-buf-1",
    }
    return cfg, atpg_view, scan_ctx, fp


@pytest.mark.integration
@pytest.mark.golden
def test_scan_loc_transition_run_accepts_and_persists_launch_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _transition_scan_workspace(tmp_path)
    assert cfg.fault_model.model == "transition"

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
    )

    # A real LOC transition vector was generated, verified, and accepted.
    assert stats.accepted_vectors >= 1

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT pattern, launch_pattern FROM vectors WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()
        campaign_model = conn.execute(
            "SELECT fault_model FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()

    assert rows, "expected at least one persisted transition vector"
    # Two-frame storage: launch_pattern (V1) populated and distinct columns set.
    assert any(
        str(row["launch_pattern"]) and set(str(row["launch_pattern"])) <= {"0", "1"}
        for row in rows
    )
    assert campaign_model is not None
    assert str(campaign_model["fault_model"]) == "transition"


@pytest.mark.integration
@pytest.mark.golden
def test_scan_transition_compaction_preserves_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _transition_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)

    vectors, _stats, run_id, _a, _s = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=3,
        target_coverage=100.0,
        transition=True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        detected_before = {
            row.fault_id for row in _detected_fault_rows(conn, campaign_id)
        }
    assert detected_before  # the campaign detected at least one transition fault

    scan_cell_map = str(resolve_scan_cell_map(cfg))
    compacted, new_run_id, raw_count = compact_run_scan_transition(
        core,
        scan_ctx=scan_ctx,
        reduced_json_path=str(atpg_view),
        reduced_cell_map=scan_cell_map,
        generic_cell_map=scan_cell_map,
        db_path=str(cfg.db_path),
        campaign_id=campaign_id,
        run_id=run_id,
        vectors=vectors,
        unsupported=cfg.simulation.unsupported_cells,
        launch_mode="loc",
    )

    # Compaction is a non-growing subset; faults stay detected (coverage held).
    assert 1 <= compacted.count <= int(raw_count)
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        detected_after = {
            row.fault_id for row in _detected_fault_rows(conn, campaign_id)
        }
        kept = conn.execute(
            "SELECT launch_pattern FROM vectors WHERE run_id = ?",
            (new_run_id,),
        ).fetchall()
    assert detected_after == detected_before
    assert kept and all(str(r["launch_pattern"]) for r in kept)
