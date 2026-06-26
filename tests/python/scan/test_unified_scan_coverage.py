from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.db import connect, init_schema
from faultflow.db.candidates import (
    CandidateCommit,
    CandidateRejection,
    commit_candidate,
    insert_pending_candidate,
    load_blocked_patterns,
)
from faultflow.runner.progressive_atpg import _should_stall, redundancy_model_id
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.detection_pipeline import (
    ScanPipelineContext,
    build_scan_pipeline_context,
    run_progressive_scan_atpg,
)
from faultflow.scan.site_resolution import build_site_key_index
from faultflow.scan.site_resolution import (
    apply_scan_execution_map,
    build_scan_execution_map,
)
from db_v3_helpers import insert_campaign, insert_fault_row, insert_run

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
RECONVERGE = ROOT / "tests/cpp/fixtures/tiny_reconverge.json"


def test_should_stall_accepts_protocol_no_progress_outcomes() -> None:
    assert _should_stall(
        detected=1,
        redundant=0,
        prev_detected=1,
        prev_redundant=0,
        round_outcomes=["protocol_no_progress", "TIMEOUT"],
    )
    assert _should_stall(
        detected=0,
        redundant=0,
        prev_detected=0,
        prev_redundant=0,
        round_outcomes=[],
    )
    assert not _should_stall(
        detected=1,
        redundant=0,
        prev_detected=1,
        prev_redundant=0,
        round_outcomes=["SAT"],
    )


def test_composite_vector_fk_rejects_cross_campaign_accepted_vector(
    tmp_path: Path,
) -> None:
    db = tmp_path / "fk.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        comb = insert_campaign(conn, campaign_type="comb")
        scan = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, comb)
        conn.execute(
            """
            INSERT INTO vectors(campaign_id, run_id, source, vector_index, pattern)
            VALUES (?, ?, 'scan_native_sat_atpg', 1, '0')
            """,
            (comb, run_id),
        )
        vector_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO atpg_candidates(
                  campaign_id, run_id, candidate_id, pattern, source, status,
                  accepted_vector_id
                ) VALUES (?, ?, 1, '0', 'sat', 'accepted', ?)
                """,
                (scan, run_id, vector_id),
            )
            conn.commit()


def test_candidate_rejection_requires_parent_candidate(tmp_path: Path) -> None:
    db = tmp_path / "reject_fk.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, campaign_id)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="undetected",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO candidate_rejections(
                  campaign_id, run_id, candidate_id, fault_id, reason_code
                ) VALUES (?, ?, 99, ?, 'no_capture_or_unload_effect')
                """,
                (campaign_id, run_id, fault_id),
            )
            conn.commit()


def test_blocked_patterns_reload_after_commit(tmp_path: Path) -> None:
    db = tmp_path / "blocked.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, campaign_id)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=3,
            net_name="n3",
            compiled_net_index=3,
            fault_type="sa1",
            status="undetected",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        insert_pending_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=1,
            pattern="10",
            source="sat",
            sat_target_fault_id=fault_id,
        )
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=1,
            commit=CandidateCommit(
                status="rejected",
                vector_index=1,
                protocol_sim_rejections=[
                    CandidateRejection(fault_id, "no_capture_or_unload_effect")
                ],
                blocked_patterns=[(fault_id, "10")],
            ),
        )
        conn.commit()
        blocked = load_blocked_patterns(conn, campaign_id)
    assert blocked[fault_id] == {"10"}


def _d_fanout_generic_and_manifest() -> tuple[dict[str, Any], dict[str, Any]]:
    generic = {
        "modules": {
            "tiny_d_fanout": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "scan_in": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "D": {"direction": "input", "bits": [5]},
                    "B": {"direction": "input", "bits": [6]},
                    "Q": {"direction": "output", "bits": [7]},
                    "scan_out": {"direction": "output", "bits": [7]},
                    "Y": {"direction": "output", "bits": [9]},
                },
                "cells": {
                    "u0": {
                        "type": "$scanff_faultflow",
                        "parameters": {},
                        "attributes": {},
                        "connections": {
                            "CLK": [2],
                            "D": [5],
                            "SDI": [3],
                            "SE": [4],
                            "Q": [7],
                        },
                    },
                    "u_and": {
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "connections": {"A": [5], "B": [6], "X": [9]},
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "B": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "Y": {"hide_name": 0, "bits": [9], "attributes": {}},
                },
            }
        }
    }
    manifest = {
        "top": "tiny_d_fanout",
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in"],
        "scan_outputs": ["scan_out"],
        "max_chain_length": 1,
        "cells": [
            {
                "instance": "u0",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 7,
                "data_net": 5,
            }
        ],
    }
    return generic, manifest


@pytest.mark.golden
def test_fanout_branch_site_keys_end_to_end_on_generic(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic, manifest = _d_fanout_generic_and_manifest()
    generic_path = tmp_path / "tiny_d_fanout_scan_generic.json"
    generic_path.write_text(json.dumps(generic, indent=2) + "\n", encoding="utf-8")
    _, port_map = build_scan_atpg_view(generic, manifest)
    boundary = port_map["u0"]["boundary"]
    branch_key = str(boundary["d_boundary_site_key"])
    stem_key = str(boundary["q_stem_site_key"])

    site_rows = core.list_site_keys(str(generic_path), str(CELL_MAP), "fail")
    keys = {str(row["site_key"]) for row in site_rows}
    assert "net:5:stem" in keys
    assert "net:5:branch:u0:D" in keys
    assert branch_key == "net:5:branch:u0:D"
    assert stem_key == "net:7:stem"

    index = build_site_key_index(core, generic_path, CELL_MAP, "fail")
    assert branch_key in index
    assert stem_key in index
    assert index[branch_key] != index[stem_key]


@pytest.mark.golden
def test_reduced_only_pseudo_sites_never_become_canonical_faults(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic, manifest = _d_fanout_generic_and_manifest()
    generic_path = tmp_path / "generic.json"
    generic_path.write_text(json.dumps(generic, indent=2) + "\n", encoding="utf-8")
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    generic_keys = {
        str(row["site_key"])
        for row in core.list_site_keys(str(generic_path), str(CELL_MAP), "fail")
    }
    reduced_rows = core.list_site_keys(str(reduced_path), str(CELL_MAP), "fail")
    ppi_net_ids = {int(entry["ppi_net_id"]) for entry in port_map.values()}
    reduced_only = [
        row
        for row in reduced_rows
        if str(row["site_key"]) not in generic_keys
        and int(row["yosys_net_id"]) in ppi_net_ids
    ]
    assert reduced_only
    assert all(str(row["site_key"]) not in generic_keys for row in reduced_only)


@pytest.mark.golden
def test_generic_canonical_faults_map_to_reduced_execution_sites(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic, manifest = _d_fanout_generic_and_manifest()
    generic_path = tmp_path / "generic.json"
    generic_path.write_text(json.dumps(generic, indent=2) + "\n", encoding="utf-8")
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    execution, exclusions = build_scan_execution_map(
        core,
        generic_path,
        reduced_path,
        CELL_MAP,
        CELL_MAP,
        "fail",
        port_map,
        manifest,
    )
    assert "net:7:stem" in execution
    assert "net:5:branch:u0:D" in execution
    assert exclusions["net:3:stem"] == "scan_chain"
    assert exclusions["net:4:stem"] == "scan_internal"

    db = tmp_path / "mapping.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan")
    core.ensure_faults_enumerated(
        str(generic_path),
        str(CELL_MAP),
        str(db),
        campaign_id,
        False,
        False,
        False,
        "fail",
    )
    with connect(db) as conn:
        init_schema(conn)
        apply_scan_execution_map(conn, campaign_id, execution, exclusions)
        missing = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM faults
            WHERE campaign_id = ? AND exclusion = 'none'
              AND atpg_compiled_net_index IS NULL
            """,
            (campaign_id,),
        ).fetchone()
        reduced_only_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM faults
            WHERE campaign_id = ? AND net_id IN (?, ?)
            """,
            (
                campaign_id,
                int(port_map["u0"]["ppi_net_id"]),
                int(port_map["u0"]["ppo_net_id"]),
            ),
        ).fetchone()
        exclusion_rows = conn.execute(
            """
            SELECT fault_site_key, exclusion
            FROM faults
            WHERE campaign_id = ?
              AND exclusion IN ('scan_internal', 'scan_chain')
            """,
            (campaign_id,),
        ).fetchall()
    assert missing is not None and int(missing["n"]) == 0
    assert reduced_only_count is not None and int(reduced_only_count["n"]) == 0
    tagged = {
        str(row["fault_site_key"]): str(row["exclusion"]) for row in exclusion_rows
    }
    assert tagged["net:3:stem"] == "scan_chain"
    assert tagged["net:4:stem"] == "scan_internal"


@pytest.mark.golden
def test_reconverge_fanout_branches_enumerate_distinct_site_keys(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    rows = core.list_site_keys(str(RECONVERGE), str(CELL_MAP), "fail")
    keys = {str(row["site_key"]) for row in rows}
    assert "net:2:stem" in keys
    assert "net:2:branch:u0:A" in keys
    assert "net:2:branch:u1:A" in keys


def _tiny_dff_scan_workspace(
    tmp_path: Path, *, wbr_model: str = "scan"
) -> tuple[Any, Path, Path, ScanPipelineContext, dict[str, object]]:
    from faultflow.config import load_config
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

    source = tmp_path / "tiny_dff.json"
    source.write_text(
        json.dumps(
            {
                "modules": {
                    "tiny_dff": {
                        "attributes": {"top": "1"},
                        "ports": {
                            "CLK": {"direction": "input", "bits": [2]},
                            "D": {"direction": "input", "bits": [3]},
                            "Q": {"direction": "output", "bits": [4]},
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
                            }
                        },
                        "netnames": {
                            "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                            "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                            "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                        },
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.ofs"
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
mode = comb
random_vectors = 0
max_rounds = 3
[wrap]
wbr_model = {wbr_model}
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, "tiny_dff")
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", generic)
    manifest = manifest_from_result(result, source, techmap, None)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": hash_file(generic),
    }
    manifest_path = cfg.scan_manifest_path
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

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
        "atpg_view_schema_ver": "scan-atpg-view-observe-buf-1",
    }
    return cfg, atpg_view, generic, scan_ctx, fp


@pytest.mark.unit
@pytest.mark.golden
def test_scan_stalled_when_protocol_sim_never_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    # wbr_model="buffer": transition (LOC) ATPG is rejected for wbr_model="scan"
    # (the 1-FF WBC delivers stimulus for a single capture only).
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(
        tmp_path, wbr_model="buffer"
    )
    core = runner_mod._load_core()
    assert core is not None

    from faultflow.runner.runner import _port_names

    input_order = _port_names(atpg_view, cfg.top, "input")

    def _sat_vector() -> dict[str, bool]:
        return {name: False for name in input_order}

    def fake_transition_solve(*_args: object, **_kwargs: object) -> dict[str, object]:
        # Transition SAT returns the launch frame (V1); capture is re-derived.
        return {"result": "SAT", "launch": _sat_vector()}

    def empty_tentative(*_args: object, **_kwargs: object) -> list[int]:
        return []

    def failing_protocol_sim(*_args: object, **kwargs: object) -> dict[str, object]:
        faults = kwargs.get("faults", [])
        assert isinstance(faults, list)
        lanes = [{"outcome": "no_capture_or_unload_effect"} for _ in faults]
        return {"batches": [{"lanes": lanes}]}

    # The protocol-no-progress STALL lives on the TRANSITION path. In stuck-at
    # mode a scan-FF Q-stem fault is graded by the reduced sim plus the
    # capture-value chain-integrity credit, so it only rarely reaches the
    # protocol-sim fallback -- and a single-FF SA design can never fully stall
    # (SA0/SA1 are complementary under the capture value, so one polarity always
    # passes). Transition Q-stem detection always routes through
    # simulate_scan_protocol_faults, so a sim that never confirms detection
    # produces a genuine STALL with rejected candidates.
    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(core, "solve_scan_transition_fault_atpg", fake_transition_solve)
    monkeypatch.setattr(
        core, "simulate_transition_tentative_preloaded", empty_tentative
    )
    monkeypatch.setattr(core, "simulate_scan_protocol_faults", failing_protocol_sim)
    monkeypatch.setattr(core, "verify_transition_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "faultflow.scan.detection_pipeline.reduced_protocol_matches",
        lambda *_a, **_k: True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    model_id = redundancy_model_id(fp)
    _, stats, run_id, _, _ = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        model_id,
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=2,
        target_coverage=100.0,
        transition=True,
        launch_mode="loc",
    )

    assert stats.terminal_reason == "STALLED"
    assert stats.protocol_no_progress_rounds > 0
    assert stats.rejected_candidates > 0
    assert stats.accepted_vectors == 0

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rejections = conn.execute(
            """
            SELECT reason_code
            FROM candidate_rejections
            WHERE campaign_id = ? AND run_id = ?
            """,
            (campaign_id, run_id),
        ).fetchall()
        pending = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM atpg_candidates
            WHERE campaign_id = ? AND run_id = ? AND status = 'pending'
            """,
            (campaign_id, run_id),
        ).fetchone()
    assert pending is not None
    assert int(pending["n"]) == 0
    assert any(
        row["reason_code"] == "no_capture_or_unload_effect" for row in rejections
    )


@pytest.mark.unit
@pytest.mark.golden
def test_golden_sequence_failure_aborts_candidate_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    from faultflow.runner.runner import RunnerError, _port_names

    input_order = _port_names(atpg_view, cfg.top, "input")
    candidate = {name: False for name in input_order}

    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        core,
        "solve_fault_atpg",
        lambda *_a, **_k: {"result": "SAT", "vector": candidate},
    )
    monkeypatch.setattr(
        "faultflow.scan.detection_pipeline.reduced_protocol_matches",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RunnerError("port=Q expected=0 actual=1")
        ),
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    with pytest.raises(RunnerError, match="golden_sequence_failed"):
        run_progressive_scan_atpg(
            cfg,
            atpg_view,
            redundancy_model_id(fp),
            campaign_id=campaign_id,
            scan_ctx=scan_ctx,
            max_rounds=1,
            target_coverage=100.0,
        )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        candidate_row = conn.execute(
            """
            SELECT status FROM atpg_candidates
            WHERE campaign_id = ? ORDER BY candidate_id LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        run_row = conn.execute(
            """
            SELECT status, candidates_aborted, vector_count
            FROM runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        detections = conn.execute(
            "SELECT COUNT(*) AS n FROM fault_detections WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()

    assert candidate_row is not None and candidate_row["status"] == "aborted"
    assert run_row is not None and run_row["status"] == "aborted"
    assert int(run_row["candidates_aborted"]) == 1
    assert int(run_row["vector_count"]) == 0
    assert detections is not None and int(detections["n"]) == 0


@pytest.mark.unit
@pytest.mark.golden
def test_tier_a_failure_uses_locked_rejection_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    from faultflow.runner.runner import _port_names

    input_order = _port_names(atpg_view, cfg.top, "input")
    candidate = {name: False for name in input_order}

    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        core,
        "solve_fault_atpg",
        lambda *_a, **_k: {"result": "SAT", "vector": candidate},
    )
    monkeypatch.setattr(core, "verify_fault_candidate", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "faultflow.scan.detection_pipeline.reduced_protocol_matches",
        lambda *_a, **_k: True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=1,
        target_coverage=100.0,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT reason_code FROM candidate_rejections
            WHERE campaign_id = ?
            """,
            (campaign_id,),
        ).fetchall()
    assert rows
    assert {str(row["reason_code"]) for row in rows} == {"tier_a_reduced_mismatch"}


@pytest.mark.unit
@pytest.mark.golden
def test_fault_dropping_skips_faults_detected_by_an_earlier_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    from faultflow.runner.runner import _port_names

    input_order = _port_names(atpg_view, cfg.top, "input")
    candidate = {name: False for name in input_order}
    solve_calls: list[int] = []

    def solve_once(
        _json: str,
        _cell: str,
        _db: str,
        fault_id: int,
        *_rest: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            row = conn.execute(
                "SELECT status FROM faults WHERE id = ?",
                (fault_id,),
            ).fetchone()
        assert row is not None and row["status"] == "undetected"
        solve_calls.append(fault_id)
        return {"result": "SAT", "vector": candidate}

    def detect_every_active(*_args: object, **_kwargs: object) -> list[int]:
        # _args[2] is the preloaded list of (fault_id, net_index, type) tuples
        preloaded = _args[2]
        assert isinstance(preloaded, list)
        return [int(t[0]) for t in preloaded]

    def pass_every_lane(*_args: object, **kwargs: object) -> dict[str, object]:
        faults = kwargs["faults"]
        assert isinstance(faults, list)
        return {"batches": [{"lanes": [{"outcome": "pass"} for _ in faults]}]}

    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(core, "solve_fault_atpg", solve_once)
    monkeypatch.setattr(core, "verify_fault_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(core, "simulate_tentative_preloaded", detect_every_active)
    monkeypatch.setattr(core, "simulate_scan_protocol_faults", pass_every_lane)
    monkeypatch.setattr(
        "faultflow.scan.detection_pipeline.reduced_protocol_matches",
        lambda *_a, **_k: True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    _, stats, _, _, _ = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=1,
        target_coverage=100.0,
    )

    assert len(solve_calls) == 1
    assert stats.accepted_vectors == 1


@pytest.mark.unit
@pytest.mark.golden
def test_q_stem_unsat_sets_protocol_unresolved_not_redundant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    q_stem = next(iter(scan_ctx.q_stem_site_keys))

    def unsat_solve(
        _json: str,
        _cell: str,
        _db: str,
        fault_id: int,
        *_rest: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            row = conn.execute(
                "SELECT fault_site_key FROM faults WHERE id = ?",
                (fault_id,),
            ).fetchone()
        if row is not None and str(row["fault_site_key"]) == q_stem:
            return {"result": "UNSAT", "vector": {}}
        return {"result": "TIMEOUT", "vector": {}}

    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(core, "solve_fault_atpg", unsat_solve)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    model_id = redundancy_model_id(fp)
    run_progressive_scan_atpg(
        cfg,
        atpg_view,
        model_id,
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=1,
        target_coverage=100.0,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        row = conn.execute(
            """
            SELECT status, protocol_unresolved, redundancy_model_id
            FROM faults
            WHERE campaign_id = ? AND fault_site_key = ?
            """,
            (campaign_id, q_stem),
        ).fetchone()
    assert row is not None
    assert row["status"] == "undetected"
    assert int(row["protocol_unresolved"]) == 1
    assert row["redundancy_model_id"] in ("", None)


@pytest.mark.unit
@pytest.mark.golden
def test_q_stem_sa_credited_from_reduced_functional_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    # Regression for the Q-stem PPO-bypass bug (dc559ca): a scan-FF Q-stem
    # stuck-at fault is observed in the reduced view because its Q net maps to a
    # PPI that fans out to the PPOs/POs. That functional detection must be
    # trusted directly -- it must NOT be discarded and re-graded by the
    # capture-value-only shortcut. The old code did `reduced_trusted = tentative
    # - qstem`, so a Q-stem fault whose own captured D equals the stuck value
    # (here all inputs are 0, so captured D=0) was missed for SA0. With the fix
    # both polarities are credited from the reduced grade, and the protocol sim
    # is never invoked for them.
    import faultflow.runner.runner as runner_mod

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, _generic, scan_ctx, fp = _tiny_dff_scan_workspace(tmp_path)
    core = runner_mod._load_core()
    assert core is not None

    from faultflow.runner.runner import _port_names

    input_order = _port_names(atpg_view, cfg.top, "input")
    candidate = {name: False for name in input_order}
    q_stem = next(iter(scan_ctx.q_stem_site_keys))

    def detect_every_active(*_args: object, **_kwargs: object) -> list[int]:
        preloaded = _args[2]
        assert isinstance(preloaded, list)
        return [int(t[0]) for t in preloaded]

    protocol_calls: list[object] = []

    def protocol_spy(*_args: object, **kwargs: object) -> dict[str, object]:
        protocol_calls.append(kwargs.get("faults"))
        faults = kwargs.get("faults", [])
        assert isinstance(faults, list)
        return {"batches": [{"lanes": [{"outcome": "fail"} for _ in faults]}]}

    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        core,
        "solve_fault_atpg",
        lambda *_a, **_k: {"result": "SAT", "vector": candidate},
    )
    monkeypatch.setattr(core, "verify_fault_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(core, "simulate_tentative_preloaded", detect_every_active)
    monkeypatch.setattr(core, "simulate_scan_protocol_faults", protocol_spy)
    monkeypatch.setattr(
        "faultflow.scan.detection_pipeline.reduced_protocol_matches",
        lambda *_a, **_k: True,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        from faultflow.db.campaign import ensure_campaign

        campaign_id = ensure_campaign(conn, "scan", fp)

    run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=1,
        target_coverage=100.0,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT fault_type, status
            FROM faults
            WHERE campaign_id = ? AND fault_site_key = ?
            """,
            (campaign_id, q_stem),
        ).fetchall()

    statuses = {str(row["fault_type"]).lower(): str(row["status"]) for row in rows}
    # Both polarities of the Q-stem detected via the reduced functional grade
    # (under the old self-capture-only bypass, sa0 would be missed here)...
    assert statuses.get("sa0") == "detected"
    assert statuses.get("sa1") == "detected"
    # ...and the per-fault protocol sim was never needed to confirm them.
    assert protocol_calls == []
