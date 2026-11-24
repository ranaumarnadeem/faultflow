from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.db import connect, init_schema
from faultflow.db.campaign import ensure_campaign
from faultflow.db.candidates import load_blocked_patterns
from faultflow.runner.progressive_atpg import pattern_key, redundancy_model_id
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
from wrap_flow import TOP as RING_TOP
from wrap_flow import ring

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


def _transition_scan_workspace(
    tmp_path: Path,
    netlist: dict[str, object] | None = None,
    top: str = "tiny_dff_inv",
    random_vectors: int = 0,
):
    source = tmp_path / f"{top}.json"
    source.write_text(
        json.dumps(netlist or _tiny_dff_inv_json(), indent=2) + "\n",
        encoding="utf-8",
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
random_vectors = {random_vectors}
max_rounds = 3
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, top)
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(source, CELL_MAP, top, generic)
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


@pytest.mark.integration
@pytest.mark.golden
def test_scan_transition_blocks_a_pattern_only_where_sat_could_return_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """GIVEN a LOC campaign whose candidates the full protocol grades for every
    active Q-stem fault, WHEN a candidate misses one, THEN it is rejected and
    blocked for that fault only where SAT could hand the pattern back: the
    fault SAT targeted, or one the reduced view (SAT's model) predicted it
    detects. Blocking every Q-stem fault a pattern missed stored the whole
    pattern once per fault: 6.7M rows, 41 GB, in 2.4 h of boxcar's LOC run
    (2026-11-23)."""
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _transition_scan_workspace(
        tmp_path, netlist=ring(), top=RING_TOP, random_vectors=16
    )
    core = runner_mod._load_core()
    assert core is not None
    predicted: dict[str, set[int]] = defaultdict(set)
    tentative = core.simulate_transition_tentative_preloaded

    def recording(path, cell_map, preloaded, vector, capture, order, *a, **kw):
        found = list(
            tentative(path, cell_map, preloaded, vector, capture, order, *a, **kw)
        )
        predicted[pattern_key(vector, order)].update(int(f) for f in found)
        return found

    monkeypatch.setattr(core, "simulate_transition_tentative_preloaded", recording)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)

    _vectors, stats, _run_id, _a, _s = run_progressive_scan_atpg(
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
        candidates = conn.execute(
            "SELECT source, pattern, sat_target_fault_id FROM atpg_candidates "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()
        blocked = load_blocked_patterns(conn, campaign_id)
        rejected = conn.execute(
            "SELECT r.fault_id, c.pattern FROM candidate_rejections r "
            "JOIN atpg_candidates c USING (campaign_id, run_id, candidate_id) "
            "WHERE r.campaign_id = ? AND r.reason_code = ?",
            (campaign_id, "no_capture_or_unload_effect"),
        ).fetchall()

    assert stats.accepted_vectors >= 1
    # random candidates were graded
    assert any(r["source"] == "random" for r in candidates) and predicted
    targets = {
        (int(r["sat_target_fault_id"]), str(r["pattern"]))
        for r in candidates
        if r["source"] == "sat"
    }

    def stray(rows: Iterable[tuple[int, str]]) -> list[tuple[int, str]]:
        return [
            (fid, pat)
            for fid, pat in rows
            if fid not in predicted.get(pat, ()) and (fid, pat) not in targets
        ]

    assert stray((f, p) for f, pats in blocked.items() for p in pats) == []
    assert stray((int(r["fault_id"]), str(r["pattern"])) for r in rejected) == []


@pytest.mark.integration
@pytest.mark.golden
def test_scan_atpg_solver_gets_every_block_stored_before_its_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """GIVEN two LOC runs on one campaign (the second resumes it) whose protocol
    sim misses every Q-stem fault, so each candidate blocks the ones the reduced
    view predicted -- random fill's included, which SAT never targeted -- WHEN
    SAT solves a fault, THEN it is handed every pattern the DB held blocked for
    that fault when the round's SAT phase began. The loop reads the table once
    per run and keeps it up to date from its own commits, where it used to
    re-read it every round; this checks the two agree, across rounds and runs."""
    import faultflow.runner.runner as runner_mod
    import faultflow.scan.detection_pipeline as pipeline

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _transition_scan_workspace(
        tmp_path, netlist=ring(), top=RING_TOP, random_vectors=16
    )
    core = runner_mod._load_core()
    assert core is not None
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)

    def every_fault_missed(*_a: Any, faults: list[Any], **_kw: Any) -> Any:
        lanes = [{"fault_index": i, "outcome": "fail"} for i in range(len(faults))]
        return {"batches": [{"lanes": lanes}]}

    stored: dict[int, set[str]] = {}
    heartbeat = pipeline.SolveHeartbeat

    def sat_phase_start(*args: Any, **kwargs: Any) -> Any:
        # Built once per round, right before the round's SAT solves.
        with connect(cfg.db_path) as conn:
            stored.clear()
            stored.update(load_blocked_patterns(conn, campaign_id))
        return heartbeat(*args, **kwargs)

    solve = core.solve_scan_transition_fault_atpg
    expected: list[tuple[int, str]] = []
    missing: list[tuple[int, str]] = []

    def checked(json_path, cell_map, db, fault_id, blocked, *a, **kw):
        want = [(int(fault_id), p) for p in stored.get(int(fault_id), ())]
        expected.extend(want)
        missing.extend((f, p) for f, p in want if p not in blocked)
        return solve(json_path, cell_map, db, fault_id, blocked, *a, **kw)

    monkeypatch.setattr(core, "simulate_scan_protocol_faults", every_fault_missed)
    monkeypatch.setattr(pipeline, "SolveHeartbeat", sat_phase_start)
    monkeypatch.setattr(core, "solve_scan_transition_fault_atpg", checked)

    first_run_checks = 0
    for run in range(2):
        if run:
            # Resume as after a run stopped before it gave up on its faults,
            # without random fill: run 2's blocks come from run 1 alone.
            with connect(cfg.db_path) as conn:
                conn.execute(
                    "UPDATE faults SET protocol_unresolved = 0 WHERE campaign_id = ?",
                    (campaign_id,),
                )
                conn.commit()
            monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
        run_progressive_scan_atpg(
            cfg,
            atpg_view,
            redundancy_model_id(fp),
            campaign_id=campaign_id,
            scan_ctx=scan_ctx,
            max_rounds=1,
            target_coverage=100.0,
            transition=True,
        )
        first_run_checks = first_run_checks or len(expected)

    # Both runs solved faults that had blocks stored, and were handed them all.
    assert 0 < first_run_checks < len(expected)
    assert missing == []


@pytest.mark.integration
@pytest.mark.golden
def test_scan_atpg_reads_the_active_faults_per_round_not_per_accepted_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """GIVEN a scan campaign whose SAT phase accepts several candidates, WHEN the
    loop moves on after an accepted candidate, THEN it takes the candidate's own
    detections out of the round's active set in memory: the active faults are
    read from the DB once per round boundary (before the first round, and at a
    round's start, after its random phase and at its end), not again after every
    accepted SAT candidate. The re-read gave the same answer for every fault the
    loop had yet to visit and cost ~5% of genericfir_small's stuck-at scan run
    (2026-11-24 profile)."""
    import faultflow.scan.detection_pipeline as pipeline

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, fp = _transition_scan_workspace(
        tmp_path, netlist=ring(), top=RING_TOP, random_vectors=0
    )
    reads = 0
    accepted_sat = 0
    real_reads = pipeline._active_fault_rows
    real_candidate = pipeline._process_scan_candidate

    def counted_reads(conn: Any, campaign_id: int) -> Any:
        nonlocal reads
        reads += 1
        return real_reads(conn, campaign_id)

    def counted_candidate(*args: Any, **kwargs: Any) -> Any:
        nonlocal accepted_sat
        result = real_candidate(*args, **kwargs)
        if kwargs.get("source") == "sat" and result[0]:
            accepted_sat += 1
        return result

    monkeypatch.setattr(pipeline, "_active_fault_rows", counted_reads)
    monkeypatch.setattr(pipeline, "_process_scan_candidate", counted_candidate)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)

    _vectors, stats, _run_id, _a, _s = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=3,
        target_coverage=100.0,
        transition=True,
    )

    assert accepted_sat >= 2
    assert reads <= 1 + 3 * stats.rounds, (
        f"{reads} reads of the active faults across {stats.rounds} rounds and "
        f"{accepted_sat} accepted SAT candidates"
    )
