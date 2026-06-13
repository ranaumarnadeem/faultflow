from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.atpg import VectorSet
from faultflow.config import FaultflowConfig
from faultflow.db import connect, init_schema, summary
from faultflow.db.candidates import (
    CandidateCommit,
    CandidateRejection,
    append_vector_row,
    commit_candidate,
    insert_pending_candidate,
    load_blocked_patterns,
)
from faultflow.runner.progressive_atpg import (
    ATPGRANDOM_SEED,
    AtpgStats,
    _RoundTracker,
    _append_unique_vectors,
    _fault_counts,
    _should_stall,
    pattern_key,
)
from faultflow.runner.runner import RunnerError
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.protocol import serialize_vector
from faultflow.scan.site_resolution import build_site_key_index, fault_type_to_sa_code
from faultflow.scan.verify import reduced_protocol_matches


@dataclass(frozen=True)
class ScanPipelineContext:
    cfg: FaultflowConfig
    manifest: dict[str, Any]
    generic_json: Path
    pseudo_port_map: dict[str, dict[str, Any]]
    functional_output_order: list[str]
    q_stem_site_keys: frozenset[str]


@dataclass
class _FaultRow:
    fault_id: int
    fault_site_key: str
    fault_type: str


def build_scan_pipeline_context(
    cfg: FaultflowConfig,
    manifest: dict[str, Any],
    generic_json: Path,
    pseudo_port_map: dict[str, dict[str, Any]],
    functional_output_order: list[str],
) -> ScanPipelineContext:
    q_stems = {
        str(entry["boundary"]["q_stem_site_key"])
        for entry in pseudo_port_map.values()
        if isinstance(entry.get("boundary"), dict)
    }
    return ScanPipelineContext(
        cfg=cfg,
        manifest=manifest,
        generic_json=generic_json,
        pseudo_port_map=pseudo_port_map,
        functional_output_order=functional_output_order,
        q_stem_site_keys=frozenset(q_stems),
    )


def _active_fault_rows(conn: sqlite3.Connection, campaign_id: int) -> list[_FaultRow]:
    rows = conn.execute(
        """
        SELECT id, fault_site_key, fault_type
        FROM faults
        WHERE campaign_id = ?
          AND status = 'undetected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
          AND protocol_unresolved = 0
        ORDER BY id
        """,
        (campaign_id,),
    ).fetchall()
    return [
        _FaultRow(
            fault_id=int(row["id"]),
            fault_site_key=str(row["fault_site_key"]),
            fault_type=str(row["fault_type"]),
        )
        for row in rows
    ]


def _active_q_stem_fault_ids(
    rows: list[_FaultRow], q_stem_site_keys: frozenset[str]
) -> list[int]:
    return [row.fault_id for row in rows if row.fault_site_key in q_stem_site_keys]


def _termination_sweep_q_stems(
    conn: sqlite3.Connection,
    campaign_id: int,
    q_stem_site_keys: frozenset[str],
) -> int:
    if not q_stem_site_keys:
        return 0
    placeholders = ",".join("?" for _ in q_stem_site_keys)
    cur = conn.execute(
        f"""
        UPDATE faults
        SET protocol_unresolved = 1
        WHERE campaign_id = ?
          AND status = 'undetected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
          AND fault_site_key IN ({placeholders})
        """,
        (campaign_id, *sorted(q_stem_site_keys)),
    )
    conn.commit()
    return int(cur.rowcount)


def _protocol_fault_sim_kwargs(ctx: ScanPipelineContext, pattern: Any) -> dict[str, object]:
    clock_net = ctx.manifest.get("clock_net")
    if not isinstance(clock_net, int):
        raise RunnerError("scan manifest clock_net must be an integer")
    from faultflow.runner.runner import _port_name_for_net

    clock_port = _port_name_for_net(
        ctx.generic_json, str(ctx.manifest["top"]), clock_net, "input"
    )
    if clock_port is None:
        raise RunnerError(f"cannot map scan clock net {clock_net} to a port")
    scan_inputs = ctx.manifest.get("scan_inputs", [])
    scan_outputs = ctx.manifest.get("scan_outputs", [])
    if not isinstance(scan_inputs, list) or not isinstance(scan_outputs, list):
        raise RunnerError("manifest scan_inputs/scan_outputs must be lists")
    return {
        "clock_port": clock_port,
        "scan_enable_port": str(ctx.manifest["scan_enable"]),
        "scan_input_ports": [str(name) for name in scan_inputs],
        "scan_output_ports": [str(name) for name in scan_outputs],
        "functional_output_ports": ctx.functional_output_order,
        "max_chain_length": int(ctx.manifest.get("max_chain_length", 0)),
        "load_seqs": pattern.load_seqs,
        "capture_pi_values": pattern.capture_pi_values,
    }


def _process_scan_candidate(
    core: Any,
    conn: sqlite3.Connection,
    *,
    scan_ctx: ScanPipelineContext,
    reduced_json_path: str,
    reduced_cell_map: str,
    generic_cell_map: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    candidate_id: int,
    vector_index: int,
    vector: dict[str, bool],
    input_order: list[str],
    source: str,
    sat_target_fault_id: int | None,
    active_rows: list[_FaultRow],
    generic_site_index: dict[str, int],
    unsupported: str,
) -> tuple[bool, bool]:
    """Return (accepted, protocol_no_progress)."""
    pattern_key_str = pattern_key(vector, input_order)
    scan_pattern = serialize_vector(vector, scan_ctx.pseudo_port_map, scan_ctx.manifest)
    insert_pending_candidate(
        conn,
        campaign_id=campaign_id,
        run_id=run_id,
        candidate_id=candidate_id,
        pattern=pattern_key_str,
        source=source,
        sat_target_fault_id=sat_target_fault_id,
    )
    conn.commit()

    protocol_ok = reduced_protocol_matches(
        scan_ctx.cfg,
        scan_ctx.manifest,
        scan_ctx.generic_json,
        scan_pattern,
        reduced_vector=vector,
        functional_output_order=scan_ctx.functional_output_order,
    )
    reduced_view_rejections: list[CandidateRejection] = []
    if not protocol_ok and source == "sat" and sat_target_fault_id is not None:
        reduced_view_rejections.append(
            CandidateRejection(sat_target_fault_id, "reduced_view_mismatch")
        )
        commit = CandidateCommit(
            status="rejected",
            vector_index=vector_index,
            reduced_view_rejections=reduced_view_rejections,
        )
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        return False, True

    if not protocol_ok and source == "random":
        commit = CandidateCommit(status="rejected", vector_index=vector_index)
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        return False, False

    if source == "sat" and sat_target_fault_id is not None:
        if not core.verify_fault_candidate(
            reduced_json_path,
            reduced_cell_map,
            db_path,
            sat_target_fault_id,
            vector,
            unsupported,
        ):
            reduced_view_rejections.append(
                CandidateRejection(sat_target_fault_id, "reduced_view_mismatch")
            )
            commit = CandidateCommit(
                status="rejected",
                vector_index=vector_index,
                reduced_view_rejections=reduced_view_rejections,
            )
            commit_candidate(
                conn,
                campaign_id=campaign_id,
                run_id=run_id,
                candidate_id=candidate_id,
                commit=commit,
            )
            return False, True

    active_ids = [row.fault_id for row in active_rows]
    tentative = list(
        core.simulate_tentative(
            reduced_json_path,
            reduced_cell_map,
            db_path,
            vector,
            input_order,
            active_ids,
            unsupported,
        )
    )
    protocol_sim_fault_ids = sorted(
        set(tentative)
        | set(_active_q_stem_fault_ids(active_rows, scan_ctx.q_stem_site_keys))
    )

    if not protocol_sim_fault_ids:
        if source == "sat" and sat_target_fault_id is not None:
            reduced_view_rejections.append(
                CandidateRejection(sat_target_fault_id, "reduced_view_mismatch")
            )
            commit = CandidateCommit(
                status="rejected",
                vector_index=vector_index,
                reduced_view_rejections=reduced_view_rejections,
            )
        else:
            commit = CandidateCommit(status="rejected", vector_index=vector_index)
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        return False, source == "sat"

    row_by_id = {row.fault_id: row for row in active_rows}
    simulated_fault_ids: list[int] = []
    protocol_fault_specs: list[tuple[int, int]] = []
    for fault_id in protocol_sim_fault_ids:
        row = row_by_id.get(fault_id)
        if row is None:
            continue
        generic_cidx = generic_site_index.get(row.fault_site_key)
        if generic_cidx is None:
            raise RunnerError(
                f"generic compiled index missing for site {row.fault_site_key}"
            )
        simulated_fault_ids.append(fault_id)
        protocol_fault_specs.append((generic_cidx, fault_type_to_sa_code(row.fault_type)))

    protocol_fault_sim_kwargs = _protocol_fault_sim_kwargs(scan_ctx, scan_pattern)
    protocol_fault_sim_result = dict(
        core.simulate_scan_protocol_faults(
            str(scan_ctx.generic_json),
            generic_cell_map,
            faults=protocol_fault_specs,
            unsupported_policy=unsupported,
            **protocol_fault_sim_kwargs,
        )
    )

    passed_fault_ids: list[int] = []
    protocol_sim_rejections: list[CandidateRejection] = []
    blocked: list[tuple[int, str]] = []
    all_lanes: list[dict[str, object]] = []
    for batch in protocol_fault_sim_result.get("batches", []):
        for lane in batch.get("lanes", []):
            all_lanes.append(dict(lane))
    for fault_id, lane in zip(simulated_fault_ids, all_lanes):
        if str(lane.get("outcome")) == "pass":
            passed_fault_ids.append(fault_id)
        else:
            protocol_sim_rejections.append(
                CandidateRejection(fault_id, "no_capture_or_unload_effect")
            )
            blocked.append((fault_id, pattern_key_str))

    if not passed_fault_ids:
        commit = CandidateCommit(
            status="rejected",
            vector_index=vector_index,
            reduced_view_rejections=reduced_view_rejections,
            protocol_sim_rejections=protocol_sim_rejections,
            blocked_patterns=blocked,
        )
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        return False, source == "sat"

    vector_id = append_vector_row(
        conn,
        campaign_id=campaign_id,
        run_id=run_id,
        source="scan_native_sat_atpg",
        vector_index=vector_index,
        pattern=pattern_key_str,
    )
    commit = CandidateCommit(
        status="accepted",
        vector_index=vector_index,
        accepted_vector_id=vector_id,
        detections=passed_fault_ids,
        protocol_sim_rejections=protocol_sim_rejections,
        blocked_patterns=blocked,
    )
    commit_candidate(
        conn,
        campaign_id=campaign_id,
        run_id=run_id,
        candidate_id=candidate_id,
        commit=commit,
    )
    core.update_run_vector_count(db_path, run_id, vector_index)
    return True, False


def run_progressive_scan_atpg(
    cfg: FaultflowConfig,
    netlist: Path,
    redundancy_model: str,
    *,
    campaign_id: int,
    scan_ctx: ScanPipelineContext,
    max_rounds: int | None = None,
    target_coverage: float | None = None,
    db_path: Path | None = None,
    cell_map_path: Path | None = None,
    vector_source: str = "scan_native_sat_atpg",
) -> tuple[VectorSet, AtpgStats, int, float, float]:
    from faultflow.runner.runner import _load_core, _port_names

    core = _load_core()
    if core is None:
        raise RunnerError(
            "C++ extension _faultflow_core is required. "
            "Run: cmake --build build -- -j2"
        )

    effective_max_rounds = max_rounds if max_rounds is not None else cfg.atpg.max_rounds
    effective_target = (
        target_coverage if target_coverage is not None else cfg.report.threshold
    )
    if effective_max_rounds < 1:
        raise RunnerError("max_rounds must be >= 1")
    if not 0.0 < effective_target <= 100.0:
        raise RunnerError("target coverage must be in (0, 100]")

    input_order = _port_names(netlist, cfg.top, "input")
    effective_db_path = str(db_path if db_path is not None else cfg.db_path)
    reduced_json_path = str(netlist)
    reduced_cell_map = str(cell_map_path if cell_map_path is not None else cfg.cell_lib)
    generic_cell_map = str(resolve_scan_cell_map(cfg))
    unsupported = cfg.simulation.unsupported_cells

    core.ensure_faults_enumerated(
        reduced_json_path,
        reduced_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
    )
    core.invalidate_stale_redundant(effective_db_path, campaign_id, redundancy_model)

    generic_site_index = build_site_key_index(
        core, scan_ctx.generic_json, generic_cell_map, unsupported
    )

    stats = AtpgStats()
    vectors: list[dict[str, bool]] = []
    seen_patterns: set[str] = set()
    rejected_patterns: dict[int, set[str]] = {}
    candidate_counter = 0

    atpg_start = time.perf_counter()
    sim_start = time.perf_counter()

    with connect(effective_db_path) as conn:
        init_schema(conn)
        conn.execute(
            """
            INSERT INTO runs(campaign_id, status, vector_source, vector_count)
            VALUES (?, 'running', ?, 0)
            """,
            (campaign_id, vector_source),
        )
        conn.commit()
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    terminal = "MAX_ROUNDS"
    for round_idx in range(1, effective_max_rounds + 1):
        stats.rounds = round_idx
        round_tracker = _RoundTracker()
        with connect(effective_db_path) as conn:
            init_schema(conn)
            prev_detected, prev_redundant = _fault_counts(conn, campaign_id)
            active_rows = _active_fault_rows(conn, campaign_id)
            db_blocked = load_blocked_patterns(conn, campaign_id)

        active_ids = [row.fault_id for row in active_rows]
        if not active_ids:
            terminal = "COMPLETE"
            break

        random_batch = core.atpg_random_vectors(
            input_order, cfg.atpg.random_vectors, ATPGRANDOM_SEED
        )
        new_random = _append_unique_vectors(
            vectors, seen_patterns, input_order, list(random_batch)
        )
        for vector in new_random:
            stats.generated_vectors += 1
            candidate_counter += 1
            vector_index = len(vectors) + 1
            with connect(effective_db_path) as conn:
                init_schema(conn)
                accepted, protocol_no_progress = _process_scan_candidate(
                    core,
                    conn,
                    scan_ctx=scan_ctx,
                    reduced_json_path=reduced_json_path,
                    reduced_cell_map=reduced_cell_map,
                    generic_cell_map=generic_cell_map,
                    db_path=effective_db_path,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    candidate_id=candidate_counter,
                    vector_index=vector_index,
                    vector=vector,
                    input_order=input_order,
                    source="random",
                    sat_target_fault_id=None,
                    active_rows=active_rows,
                    generic_site_index=generic_site_index,
                    unsupported=unsupported,
                )
            if accepted:
                seen_patterns.add(pattern_key(vector, input_order))
                vectors.append(vector)
                stats.accepted_vectors += 1
            elif protocol_no_progress:
                round_tracker.sat_outcomes.append("protocol_no_progress")
                stats.protocol_no_progress_rounds += 1
                stats.rejected_candidates += 1

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            db_blocked = load_blocked_patterns(conn, campaign_id)

        for row in active_rows:
            fault_id = row.fault_id
            blocked = sorted(
                db_blocked.get(fault_id, set()) | rejected_patterns.get(fault_id, set())
            )
            solved = dict(
                core.solve_fault_atpg(
                    reduced_json_path,
                    reduced_cell_map,
                    effective_db_path,
                    fault_id,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    cfg.atpg.sat_timeout_seconds,
                    unsupported,
                )
            )
            result = str(solved["result"])
            if result == "SAT":
                stats.sat += 1
                candidate = dict(solved["vector"])
                stats.generated_vectors += 1
                key = pattern_key(candidate, input_order)
                if key in seen_patterns:
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1
                    continue
                candidate_counter += 1
                vector_index = len(vectors) + 1
                with connect(effective_db_path) as conn:
                    init_schema(conn)
                    accepted, protocol_no_progress = _process_scan_candidate(
                        core,
                        conn,
                        scan_ctx=scan_ctx,
                        reduced_json_path=reduced_json_path,
                        reduced_cell_map=reduced_cell_map,
                        generic_cell_map=generic_cell_map,
                        db_path=effective_db_path,
                        campaign_id=campaign_id,
                        run_id=run_id,
                        candidate_id=candidate_counter,
                        vector_index=vector_index,
                        vector=candidate,
                        input_order=input_order,
                        source="sat",
                        sat_target_fault_id=fault_id,
                        active_rows=active_rows,
                        generic_site_index=generic_site_index,
                        unsupported=unsupported,
                    )
                if accepted:
                    seen_patterns.add(key)
                    vectors.append(candidate)
                    stats.accepted_vectors += 1
                    round_tracker.sat_outcomes.append("SAT")
                else:
                    stats.rejected_candidates += 1
                    rejected_patterns.setdefault(fault_id, set()).add(key)
                    round_tracker.sat_outcomes.append(
                        "protocol_no_progress" if protocol_no_progress else "SAT"
                    )
                    if protocol_no_progress:
                        stats.protocol_no_progress_rounds += 1
            elif result == "UNSAT":
                stats.unsat += 1
                if row.fault_site_key in scan_ctx.q_stem_site_keys:
                    core.mark_fault_protocol_unresolved(effective_db_path, fault_id)
                else:
                    core.mark_fault_redundant(
                        effective_db_path, fault_id, redundancy_model
                    )
            elif result == "TIMEOUT":
                stats.timeout += 1
                round_tracker.sat_outcomes.append("TIMEOUT")
            else:
                stats.unknown += 1
                round_tracker.sat_outcomes.append("UNKNOWN")

        with connect(effective_db_path) as conn:
            init_schema(conn)
            data = summary(conn, campaign_id=campaign_id)
            detected, redundant = _fault_counts(conn, campaign_id)
            active_remaining = len(_active_fault_rows(conn, campaign_id))
            coverage = data["coverage_percent"]

        if active_remaining == 0:
            terminal = "COMPLETE"
            break
        if coverage is not None and coverage >= effective_target:
            terminal = "THRESHOLD_MET"
            break
        if _should_stall(
            detected=detected,
            redundant=redundant,
            prev_detected=prev_detected,
            prev_redundant=prev_redundant,
            round_outcomes=round_tracker.sat_outcomes,
        ):
            with connect(effective_db_path) as conn:
                init_schema(conn)
                _termination_sweep_q_stems(conn, campaign_id, scan_ctx.q_stem_site_keys)
            terminal = "STALLED"
            break

    atpg_seconds = time.perf_counter() - atpg_start
    fault_sim_seconds = time.perf_counter() - sim_start

    with connect(effective_db_path) as conn:
        init_schema(conn)
        if terminal == "MAX_ROUNDS":
            _termination_sweep_q_stems(conn, campaign_id, scan_ctx.q_stem_site_keys)
        data = summary(conn, campaign_id=campaign_id)
        if data["denominator"] == 0:
            raise RunnerError("denominator is zero after progressive ATPG")
        core.complete_run_with_atpg(
            effective_db_path,
            run_id,
            float(data["coverage_percent"] or 0.0),
            terminal,
            stats.rounds,
            stats.sat,
            stats.unsat,
            stats.timeout,
            stats.unknown,
            stats.rejected_candidates,
            stats.generated_vectors,
            stats.accepted_vectors,
        )

    stats.terminal_reason = terminal
    return (
        VectorSet(vector_source, input_order, vectors),
        stats,
        run_id,
        atpg_seconds,
        fault_sim_seconds,
    )
