"""Scan ATPG: OpenTestability's preflight orders work but never classifies a
fault. A structural reconvergence record is not a redundancy proof -- only a
SAT UNSAT under the current model is -- and a branch fault reaches one reader,
so it can be testable where its stem is not. See preflight_fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.db import connect, init_schema
from faultflow.db.campaign import ensure_campaign
from faultflow.runner.progressive_atpg import redundancy_model_id
from faultflow.scan.atpg_view import ATPG_VIEW_SCHEMA_VER, build_scan_atpg_view
from faultflow.scan.detection_pipeline import (
    build_scan_pipeline_context,
    run_progressive_scan_atpg,
)
from preflight_fixtures import (
    TOP,
    assert_only_proven_faults_redundant,
    fault_statuses,
    install_fake_opentest,
    write_reconvergent_netlist,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _scan_workspace(root: Path, *, preflight: bool) -> tuple[Any, Path, Any, dict]:
    from faultflow.config import load_config
    from faultflow.runner.runner import _port_names
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

    source = write_reconvergent_netlist(root / f"{TOP}.json", with_flop=True)
    cfg_path = root / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {source}
cell_lib = {CELL_MAP}
[fault_model]
collapsing = false
[simulation]
unsupported_cells = fail
[atpg]
random_vectors = 0
max_rounds = 3
preflight = {str(preflight).lower()}
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, TOP)
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(source, CELL_MAP, TOP, generic)
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
    fp = {
        "top": cfg.top,
        "netlist_hash": "scan-test",
        "cell_lib_hash": "cell-test",
        "config_hash": "cfg-test",
        "template_hash": "tmpl-test",
        "yosys_version": "yosys",
        "faultflow_version": "test",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
        "manifest_hash": str(manifest.get("generic_json_hash", "")),
        "atpg_view_schema_ver": ATPG_VIEW_SCHEMA_VER,
    }
    return cfg, atpg_view, scan_ctx, fp


def _run_scan_atpg(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, preflight: bool
) -> dict[tuple[str, str], str]:
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    cfg, atpg_view, scan_ctx, fp = _scan_workspace(root, preflight=preflight)
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)
    run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=3,
        target_coverage=100.0,
    )
    return fault_statuses(cfg.db_path, campaign_id)


@pytest.mark.unit
@pytest.mark.golden
def test_scan_preflight_reconvergent_stems_mark_nothing_redundant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """GIVEN OT reports stems A and S of the reconvergent netlist
    WHEN scan ATPG runs with that preflight, and again without it
    THEN only SAT-proven faults are redundant (A's stem), every A branch and
    every S fault is detected, and no verdict differs between the runs."""
    calls = install_fake_opentest(monkeypatch)

    hinted = _run_scan_atpg(tmp_path / "hinted", monkeypatch, preflight=True)
    reference = _run_scan_atpg(tmp_path / "reference", monkeypatch, preflight=False)

    assert len(calls) == 1  # the hinted run really consumed OT's records
    assert_only_proven_faults_redundant(hinted, reference)
