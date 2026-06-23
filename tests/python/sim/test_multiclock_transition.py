"""Phase 6b — multi-clock transition fault validation.

Design: multi_clock_2domain (2-domain, clk_a / clk_b)
  clk_a domain: ff0_a (sdf_a0), ff1_a (sdf_a1)
  clk_b domain: ff0_b (sdf_b0)
  cross-domain path: q1_a (net 13) -> u_and -> ff0_b.D (net 14)

Tests:
  1. Cross-domain classifier: net 14 (and_out) is excluded_cross_domain
  2. Per-domain ATPG view: inactive-domain FFs have no PPO
  3. C++ simulation: clk_a-only LOC leaves domain-B FF state unchanged
  4. C++ simulation: clk_b-only LOC leaves domain-A FF states unchanged
  5. Full pipeline: transition ATPG with 2 clocks, excluded_cross_domain counted
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
PRE_STITCH = ROOT / "tests/fixtures/multi_clock/pre_stitch.json"
POST_STITCH = ROOT / "tests/fixtures/multi_clock/two_domain_3ff.json"
TOP = "multi_clock_2domain"

# Minimal manifest for two_domain_3ff.json (instance names ff0_a / ff1_a / ff0_b)
_TWO_DOMAIN_MANIFEST: dict[str, Any] = {
    "top": TOP,
    "clock_nets": [2, 3],
    "scan_enable": "scan_en",
    "scan_inputs": ["scan_in_a", "scan_in_b"],
    "scan_outputs": ["scan_out_a", "scan_out_b"],
    "max_chain_length": 2,
    "cells": [
        {
            "instance": "ff0_a",
            "chain_index": 0,
            "chain_position": 0,
            "clock_net": 2,
            "q_net": 11,
            "data_net": 7,
            "scan_in_net": 5,
            "scan_enable_net": 4,
        },
        {
            "instance": "ff1_a",
            "chain_index": 0,
            "chain_position": 1,
            "clock_net": 2,
            "q_net": 13,
            "data_net": 12,
            "scan_in_net": 11,
            "scan_enable_net": 4,
        },
        {
            "instance": "ff0_b",
            "chain_index": 1,
            "chain_position": 0,
            "clock_net": 3,
            "q_net": 15,
            "data_net": 14,
            "scan_in_net": 6,
            "scan_enable_net": 4,
        },
    ],
}


# ---------------------------------------------------------------------------
# Layer 1 — cross-domain classifier (no C++ core required)
# ---------------------------------------------------------------------------


def test_cross_domain_classifier_identifies_and_out_net() -> None:
    """Net 14 (and_out) sits between clk_a launch and clk_b capture — excluded."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(POST_STITCH, _TWO_DOMAIN_MANIFEST)
    assert (
        14 in cross
    ), f"expected net 14 (and_out) in cross-domain set; got {sorted(cross)}"


def test_cross_domain_classifier_intra_domain_nets_not_excluded() -> None:
    """Intra-domain nets (q0_a, xor_out, q1_a/PO, q0_b/PO) must NOT be excluded."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(POST_STITCH, _TWO_DOMAIN_MANIFEST)
    # net 11 (q0_a): clk_a launch → xor → ff1_a.D (clk_a capture) ✓
    # net 12 (xor_out): clk_a launch chain, captured at ff1_a.D ✓
    # net 13 (q1_a / scan_out_a PO): captured by PO ✓
    # net 15 (q0_b / scan_out_b PO): captured by PO ✓
    for net_id in (11, 12, 13, 15):
        assert (
            net_id not in cross
        ), f"net {net_id} should be intra-domain testable but found in cross-domain set"


def test_cross_domain_classifier_single_domain_returns_empty() -> None:
    """With only one clock domain, nothing is cross-domain."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    single_manifest = dict(_TWO_DOMAIN_MANIFEST)
    single_manifest["clock_nets"] = [2]
    cross = compute_cross_domain_net_ids(POST_STITCH, single_manifest)
    assert cross == set()


def test_tag_cross_domain_exclusions_adds_entries() -> None:
    """tag_cross_domain_exclusions should add cross_domain for matched net IDs."""
    from faultflow.scan.domain_reach import tag_cross_domain_exclusions

    rows = [
        {"site_key": "stem:14", "yosys_net_id": 14},
        {"site_key": "branch:14:sdf_b0:D", "yosys_net_id": 14},
        {"site_key": "stem:12", "yosys_net_id": 12},
    ]
    result = tag_cross_domain_exclusions(rows, {14}, {})
    assert result["stem:14"] == "cross_domain"
    assert result["branch:14:sdf_b0:D"] == "cross_domain"
    assert "stem:12" not in result


def test_tag_cross_domain_exclusions_respects_existing_tags() -> None:
    """Existing exclusion tags (scan_internal) are not overwritten."""
    from faultflow.scan.domain_reach import tag_cross_domain_exclusions

    rows = [{"site_key": "stem:14", "yosys_net_id": 14}]
    # pre-tagged as scan_internal
    result = tag_cross_domain_exclusions(rows, {14}, {"stem:14": "scan_internal"})
    assert result["stem:14"] == "scan_internal"


# ---------------------------------------------------------------------------
# Layer 2 — per-domain ATPG view (no C++ core required)
# ---------------------------------------------------------------------------


def test_atpg_view_clk_a_active_masks_domain_b_ppo() -> None:
    """Active clk_a view: sdf_b0 gets PPI but no PPO."""
    from faultflow.scan.atpg_view import build_scan_atpg_view, PPO_PREFIX

    view, ppm = build_scan_atpg_view(
        json.loads(POST_STITCH.read_text(encoding="utf-8")),
        _TWO_DOMAIN_MANIFEST,
        active_clock_net=2,
    )
    # ff0_b (clock_net=3) should have ppo_port=None
    assert ppm["ff0_b"]["ppo_port"] is None
    # ff0_a and ff1_a (clock_net=2) should have a PPO port
    assert ppm["ff0_a"]["ppo_port"] is not None
    assert ppm["ff0_a"]["ppo_port"].startswith(PPO_PREFIX)
    assert ppm["ff1_a"]["ppo_port"] is not None


def test_atpg_view_clk_b_active_masks_domain_a_ppos() -> None:
    """Active clk_b view: sdf_a0 and sdf_a1 get PPI but no PPO."""
    from faultflow.scan.atpg_view import build_scan_atpg_view, PPO_PREFIX

    view, ppm = build_scan_atpg_view(
        json.loads(POST_STITCH.read_text(encoding="utf-8")),
        _TWO_DOMAIN_MANIFEST,
        active_clock_net=3,
    )
    assert ppm["ff0_a"]["ppo_port"] is None
    assert ppm["ff1_a"]["ppo_port"] is None
    assert ppm["ff0_b"]["ppo_port"] is not None
    assert ppm["ff0_b"]["ppo_port"].startswith(PPO_PREFIX)


def test_atpg_view_no_active_clock_all_ffs_get_ppo() -> None:
    """Without active_clock_net, all FFs get a PPO (single-domain / regression)."""
    from faultflow.scan.atpg_view import build_scan_atpg_view

    view, ppm = build_scan_atpg_view(
        json.loads(POST_STITCH.read_text(encoding="utf-8")),
        _TWO_DOMAIN_MANIFEST,
    )
    for inst in ("ff0_a", "ff1_a", "ff0_b"):
        assert ppm[inst]["ppo_port"] is not None, f"{inst} missing PPO"


# ---------------------------------------------------------------------------
# Layer 3 — C++ staggered launch (active_clock_ports)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_loc_clk_a_only_domain_b_ff_holds(require_cpp_core: None) -> None:
    """LOC with active_clock_ports=['clk_a']: ff0_b.Q holds loaded value after capture.

    Chain B has 1 FF but max_chain_length=2, so unload_seqs[1] has 2 elements.
    The FIRST element (unload[1][0]) is ff0_b.Q after the capture phase.
    """
    import _faultflow_core as core  # type: ignore[import-not-found]

    # Load chain-A: [false, false] (both A FFs = 0)
    # Load chain-B: [false, true] → last scan pulse carries True → ff0_b.Q=True
    result = core.simulate_scan_pattern(
        str(POST_STITCH),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["Q0_A"],
        max_chain_length=2,
        load_seqs={0: [False, False], 1: [False, True]},
        capture_pi_values={"d0": False, "d1": False, "d2": False},
        active_clock_ports=["clk_a"],
        loc_two_capture=True,
    )
    unload = {int(k): v for k, v in result["unload_seqs"].items()}
    # Chain 1 (clk_b): ff0_b.Q loaded True; clk_b held during LOC → still True.
    # unload[1][0] is ff0_b.Q after the capture phase.
    assert (
        unload[1][0] is True
    ), f"domain-B FF should hold True during clk_a-only LOC; got {unload[1]}"


@pytest.mark.golden
def test_loc_clk_b_only_domain_a_ffs_hold(require_cpp_core: None) -> None:
    """LOC with active_clock_ports=['clk_b']: domain-A FFs hold their loaded values.

    Mirror of the C++ stagger test 6B-C03.
    Chain-A load: [False, True] = [ff1_a=0, ff0_a=1].
    With clk_b-only LOC, clk_a doesn't fire → ff0_a stays 1, ff1_a stays 0.
    Unload chain-A: [ff1_a.Q, ff0_a.Q] = [False, True].
    """
    import _faultflow_core as core  # type: ignore[import-not-found]

    # Load chain-A: [False, True] → ff1_a=0, ff0_a=1 (ff0_a is position 0 in chain)
    result = core.simulate_scan_pattern(
        str(POST_STITCH),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["Q0_A"],
        max_chain_length=2,
        load_seqs={0: [False, True], 1: [False]},
        capture_pi_values={"d0": False, "d1": False, "d2": False},
        active_clock_ports=["clk_b"],
        loc_two_capture=True,
    )
    unload = {int(k): v for k, v in result["unload_seqs"].items()}
    # Chain 0 (clk_a domain): ff1_a held 0, ff0_a held 1.
    # Unload order: last FF (ff1_a) first, then ff0_a propagates through.
    assert len(unload[0]) == 2, f"chain A must have 2 unload bits, got {unload[0]}"
    assert (
        unload[0][0] is False
    ), f"ff1_a should hold 0 (loaded) during clk_b-only LOC; got {unload[0][0]}"
    assert (
        unload[0][1] is True
    ), f"ff0_a should hold 1 (loaded) during clk_b-only LOC; got {unload[0][1]}"


@pytest.mark.golden
def test_loc_all_clocks_active_baseline(require_cpp_core: None) -> None:
    """Without active_clock_ports restriction, baseline simulation completes."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    result = core.simulate_scan_pattern(
        str(POST_STITCH),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["Q0_A"],
        max_chain_length=2,
        load_seqs={0: [False, True], 1: [False, True]},
        capture_pi_values={"d0": False, "d1": False, "d2": False},
        active_clock_ports=[],  # all clocks active = regression-safe default
    )
    assert "real_po_values" in result
    assert "unload_seqs" in result


# ---------------------------------------------------------------------------
# Layer 4 — full pipeline (transition ATPG with 2 domains)
# ---------------------------------------------------------------------------


def _two_domain_transition_workspace(
    tmp_path: Path,
) -> tuple[Any, Path, Any, dict[str, Any]]:
    """Build a scan transition workspace from pre_stitch.json (2-domain)."""
    from faultflow.config import load_config
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.atpg_view import build_scan_atpg_view
    from faultflow.scan.detection_pipeline import build_scan_pipeline_context
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp
    from faultflow.runner.runner import _port_names

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {PRE_STITCH}
cell_lib = {CELL_MAP}
[clocks]
ports = clk_a, clk_b
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
    cfg = load_config(cfg_path, TOP)
    cfg.ensure_workspace()

    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(PRE_STITCH, CELL_MAP, TOP, generic, scan_chains=2)
    manifest = manifest_from_result(result, PRE_STITCH, techmap, None)
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
        "netlist_hash": "scan-mc-trans",
        "cell_lib_hash": "cell-test",
        "config_hash": "cfg-mc-test",
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
def test_multiclock_transition_atpg_produces_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """Full pipeline: multi-clock transition ATPG produces at least one vector."""
    from faultflow.db import connect, init_schema
    from faultflow.db.campaign import ensure_campaign
    from faultflow.runner.progressive_atpg import redundancy_model_id
    from faultflow.scan.detection_pipeline import run_progressive_scan_atpg

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _two_domain_transition_workspace(tmp_path)

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

    assert stats.accepted_vectors >= 1, "expected at least one transition vector"


@pytest.mark.integration
@pytest.mark.golden
def test_multiclock_transition_excluded_cross_domain_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """excluded_cross_domain appears in coverage summary for 2-domain transition run."""
    from faultflow.db import connect, init_schema, summary
    from faultflow.db.campaign import ensure_campaign
    from faultflow.runner.progressive_atpg import redundancy_model_id
    from faultflow.scan.detection_pipeline import run_progressive_scan_atpg

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _two_domain_transition_workspace(tmp_path)

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
        transition=True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn, campaign_id=campaign_id)

    assert data.get("excluded_cross_domain", 0) > 0, (
        "expected cross-domain transition faults to be tagged excluded_cross_domain; "
        f"summary={data}"
    )
    # Invariant: total_raw = denominator + redundant + collapsed + all excluded_*
    xd = data.get("excluded_cross_domain", 0)
    invariant = (
        data["denominator"]
        + data.get("redundant", 0)
        + data["collapsed"]
        + data["excluded_blackbox"]
        + data["excluded_clock"]
        + data["excluded_reset"]
        + data["excluded_scan"]
        + xd
    )
    assert (
        data["total_raw_faults"] == invariant
    ), f"coverage invariant broken: total={data['total_raw_faults']} vs sum={invariant}"
