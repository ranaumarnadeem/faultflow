from __future__ import annotations

import logging
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
    _fault_counts,
    _should_stall,
    pattern_key,
)
from faultflow.runner.runner import RunnerError
from faultflow.scan.atpg_view import PPI_PREFIX, PPO_PREFIX
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.protocol import serialize_vector
from faultflow.scan.site_resolution import build_site_key_index, fault_type_to_sa_code
from faultflow.scan.site_resolution import (
    apply_scan_execution_map,
    build_scan_execution_map,
)
from faultflow.scan.verify import reduced_protocol_matches

log = logging.getLogger(__name__)


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


def _detected_fault_rows(conn: sqlite3.Connection, campaign_id: int) -> list[_FaultRow]:
    rows = conn.execute(
        """
        SELECT id, fault_site_key, fault_type
        FROM faults
        WHERE campaign_id = ?
          AND status = 'detected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
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


def _protocol_fault_sim_kwargs(
    ctx: ScanPipelineContext, pattern: Any
) -> dict[str, object]:
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


def _materialize_reduced_outputs(
    core: Any,
    reduced_json_path: str,
    reduced_cell_map: str,
    vector: dict[str, bool],
    input_order: list[str],
    functional_output_order: list[str],
    pseudo_port_map: dict[str, dict[str, Any]],
    unsupported: str,
) -> dict[str, bool]:
    ppo_order = [str(entry["ppo_port"]) for _, entry in sorted(pseudo_port_map.items())]
    output_order = [*functional_output_order, *ppo_order]
    samples = list(
        core.fault_free_outputs(
            reduced_json_path,
            reduced_cell_map,
            [vector],
            input_order,
            output_order,
            unsupported,
        )
    )
    if len(samples) != 1:
        raise RunnerError(
            "simulator_error: reduced fault-free simulation returned "
            f"{len(samples)} samples for one candidate"
        )
    materialized = dict(vector)
    materialized.update(
        {str(name): bool(value) for name, value in dict(samples[0]).items()}
    )
    return materialized


def _derive_loc_capture(
    materialized_launch: dict[str, bool],
    launch_vector: dict[str, bool],
    pseudo_port_map: dict[str, dict[str, Any]],
) -> dict[str, bool]:
    """LOC capture vector V2 over reduced PIs: each scan FF's capture-frame PPI
    is its launch-frame PPO (next-state); real PIs are held from V1."""
    capture = {
        name: bool(value)
        for name, value in launch_vector.items()
        if not str(name).startswith(PPI_PREFIX) and not str(name).startswith(PPO_PREFIX)
    }
    for entry in pseudo_port_map.values():
        capture[str(entry["ppi_port"])] = bool(
            materialized_launch.get(str(entry["ppo_port"]), False)
        )
    return capture


def build_los_couples(
    pseudo_port_map: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str]], list[str], dict[int, str]]:
    """Return (couple_ports, head_ppi_ports, head_ppi_by_chain) for LOS.

    couple_ports[i] = (capture_ppi_port, predecessor_ppi_port) for every scan FF
    at chain position > 0; head_ppi_ports lists the position-0 (chain-head) PPI
    ports (free launch scan-in bits); head_ppi_by_chain maps chain_id -> head PPI
    port (to read the SAT-chosen head bit per chain)."""
    by_pos: dict[tuple[int, int], dict[str, Any]] = {
        (int(e["chain_id"]), int(e["position_in_chain"])): e
        for e in pseudo_port_map.values()
    }
    couple_ports: list[tuple[str, str]] = []
    head_ports: list[str] = []
    head_by_chain: dict[int, str] = {}
    for entry in pseudo_port_map.values():
        chain = int(entry["chain_id"])
        pos = int(entry["position_in_chain"])
        ppi = str(entry["ppi_port"])
        if pos == 0:
            head_ports.append(ppi)
            head_by_chain[chain] = ppi
        else:
            pred = by_pos[(chain, pos - 1)]
            couple_ports.append((ppi, str(pred["ppi_port"])))
    return couple_ports, head_ports, head_by_chain


def _derive_los_capture(
    launch_vector: dict[str, bool],
    head_scan_in_by_chain: dict[int, bool],
    pseudo_port_map: dict[str, dict[str, Any]],
) -> dict[str, bool]:
    """LOS capture vector V2 over reduced PIs: each scan FF's capture-frame PPI is
    its chain predecessor's LAUNCH PPI (the shift V2[p]=V1[p-1]); the chain head's
    PPI is the fresh launch scan-in bit; real PIs are held from V1."""
    capture = {
        name: bool(value)
        for name, value in launch_vector.items()
        if not str(name).startswith(PPI_PREFIX) and not str(name).startswith(PPO_PREFIX)
    }
    by_pos: dict[tuple[int, int], dict[str, Any]] = {
        (int(e["chain_id"]), int(e["position_in_chain"])): e
        for e in pseudo_port_map.values()
    }
    for entry in pseudo_port_map.values():
        chain = int(entry["chain_id"])
        pos = int(entry["position_in_chain"])
        ppi = str(entry["ppi_port"])
        if pos == 0:
            capture[ppi] = bool(head_scan_in_by_chain.get(chain, False))
        else:
            pred = by_pos[(chain, pos - 1)]
            capture[ppi] = bool(launch_vector.get(str(pred["ppi_port"]), False))
    return capture


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
    transition: bool = False,
    launch_mode: str = "loc",
    los_head_scan_in: dict[int, bool] | None = None,
    candidate_key: str | None = None,
) -> tuple[bool, bool]:
    """Return (accepted, protocol_no_progress).

    `vector` is the launch state V1 (over reduced PIs). For transition the capture
    vector V2 is derived from V1: LOC couples capture PPI = launch PPO; LOS shifts,
    capture PPI = predecessor's launch PPI plus the per-chain head scan-in bit
    (`los_head_scan_in`). The reduced-view expectation, scan-pattern unload, and
    golden gate use the frame-1 (V2) materialization, and grading runs two captures.
    """
    is_los = transition and launch_mode == "los"
    is_loc = transition and launch_mode == "loc"
    head_bits = los_head_scan_in or {}
    pattern_key_str = pattern_key(vector, input_order)
    # Tracking/blocking key: V1 for LOC/stuck-at; V1 ‖ head-bits for LOS (V2 is
    # shift(V1) plus the free head bits, so V1 alone is not unique). Distinct from
    # launch_pattern, which is always the length-N V1 launch frame.
    track_key = candidate_key if candidate_key is not None else pattern_key_str
    materialized_launch = _materialize_reduced_outputs(
        core,
        reduced_json_path,
        reduced_cell_map,
        vector,
        input_order,
        scan_ctx.functional_output_order,
        scan_ctx.pseudo_port_map,
        unsupported,
    )
    if transition:
        if is_los:
            capture_vector = _derive_los_capture(
                vector, head_bits, scan_ctx.pseudo_port_map
            )
        else:
            capture_vector = _derive_loc_capture(
                materialized_launch, vector, scan_ctx.pseudo_port_map
            )
        # Frame-1 outputs: PPO unload + functional PO response after the launch.
        reduced_expectation = _materialize_reduced_outputs(
            core,
            reduced_json_path,
            reduced_cell_map,
            capture_vector,
            input_order,
            scan_ctx.functional_output_order,
            scan_ctx.pseudo_port_map,
            unsupported,
        )
        # serialize: load from V1's PPIs, unload-expectation from V2's PPOs.
        serialize_input = dict(vector)
        for entry in scan_ctx.pseudo_port_map.values():
            serialize_input[str(entry["ppo_port"])] = bool(
                reduced_expectation.get(str(entry["ppo_port"]), False)
            )
        vector_pattern = pattern_key(capture_vector, input_order)
        launch_pattern = pattern_key_str
    else:
        capture_vector = {}
        reduced_expectation = materialized_launch
        serialize_input = materialized_launch
        vector_pattern = pattern_key_str
        launch_pattern = ""

    scan_pattern = serialize_vector(
        serialize_input,
        scan_ctx.pseudo_port_map,
        scan_ctx.manifest,
    )
    insert_pending_candidate(
        conn,
        campaign_id=campaign_id,
        run_id=run_id,
        candidate_id=candidate_id,
        pattern=track_key,
        source=source,
        sat_target_fault_id=sat_target_fault_id,
    )
    conn.commit()

    try:
        reduced_protocol_matches(
            scan_ctx.cfg,
            scan_ctx.manifest,
            scan_ctx.generic_json,
            scan_pattern,
            reduced_vector=reduced_expectation,
            functional_output_order=scan_ctx.functional_output_order,
            loc_two_capture=is_loc,
            los_two_capture=is_los,
            los_launch_scan_in=head_bits,
        )
    except Exception as exc:
        conn.execute(
            """
            UPDATE atpg_candidates
            SET status = 'aborted'
            WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
            """,
            (campaign_id, run_id, candidate_id),
        )
        conn.execute(
            """
            UPDATE runs
            SET status = 'aborted', completed_at = CURRENT_TIMESTAMP,
                candidates_aborted = candidates_aborted + 1
            WHERE id = ? AND campaign_id = ?
            """,
            (run_id, campaign_id),
        )
        conn.commit()
        raise RunnerError(f"golden_sequence_failed: {exc}") from exc
    reduced_view_rejections: list[CandidateRejection] = []

    if source == "sat" and sat_target_fault_id is not None:
        if transition:
            reduced_detected = core.verify_transition_candidate(
                reduced_json_path,
                reduced_cell_map,
                db_path,
                sat_target_fault_id,
                vector,
                capture_vector,
                unsupported,
            )
        else:
            reduced_detected = core.verify_fault_candidate(
                reduced_json_path,
                reduced_cell_map,
                db_path,
                sat_target_fault_id,
                vector,
                unsupported,
            )
        if not reduced_detected:
            reduced_view_rejections.append(
                CandidateRejection(sat_target_fault_id, "tier_a_reduced_mismatch")
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
    if transition:
        tentative = list(
            core.simulate_transition_tentative(
                reduced_json_path,
                reduced_cell_map,
                db_path,
                vector,
                capture_vector,
                input_order,
                active_ids,
                unsupported,
            )
        )
    else:
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
                CandidateRejection(sat_target_fault_id, "tier_a_reduced_mismatch")
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
        protocol_fault_specs.append(
            (generic_cidx, fault_type_to_sa_code(row.fault_type))
        )

    protocol_fault_sim_kwargs = _protocol_fault_sim_kwargs(scan_ctx, scan_pattern)
    protocol_fault_sim_result = dict(
        core.simulate_scan_protocol_faults(
            str(scan_ctx.generic_json),
            generic_cell_map,
            faults=protocol_fault_specs,
            unsupported_policy=unsupported,
            loc_two_capture=is_loc,
            los_two_capture=is_los,
            los_launch_scan_in=head_bits,
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
            blocked.append((fault_id, track_key))

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
        pattern=vector_pattern,
        launch_pattern=launch_pattern,
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
    conn.execute(
        "UPDATE runs SET vector_count = ? WHERE id = ?",
        (vector_index, run_id),
    )
    conn.commit()
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
    transition: bool = False,
    launch_mode: str = "loc",
) -> tuple[VectorSet, AtpgStats, int, float, float]:
    from faultflow.runner.runner import _load_core, _port_names

    los = transition and launch_mode == "los"
    los_couple_ports: list[tuple[str, str]] = []
    los_head_ports: list[str] = []
    los_head_by_chain: dict[int, str] = {}
    if los:
        los_couple_ports, los_head_ports, los_head_by_chain = build_los_couples(
            scan_ctx.pseudo_port_map
        )

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
    scan_cell_map = resolve_scan_cell_map(cfg)
    reduced_cell_map = str(
        cell_map_path if cell_map_path is not None else scan_cell_map
    )
    generic_cell_map = str(scan_cell_map)
    unsupported = cfg.simulation.unsupported_cells

    core.ensure_faults_enumerated(
        str(scan_ctx.generic_json),
        generic_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
    )
    execution_map, exclusions = build_scan_execution_map(
        core,
        scan_ctx.generic_json,
        reduced_json_path,
        generic_cell_map,
        reduced_cell_map,
        unsupported,
        scan_ctx.pseudo_port_map,
        scan_ctx.manifest,
    )
    with connect(effective_db_path) as conn:
        init_schema(conn)
        apply_scan_execution_map(
            conn,
            campaign_id,
            execution_map,
            exclusions,
        )
    core.invalidate_stale_redundant(effective_db_path, campaign_id, redundancy_model)

    generic_site_index = build_site_key_index(
        core, scan_ctx.generic_json, generic_cell_map, unsupported
    )

    with connect(effective_db_path) as conn:
        init_schema(conn)
        _pre = summary(conn, campaign_id=campaign_id)
        _initial_active = _active_fault_rows(conn, campaign_id)
    log.info(
        "atpg   %d active faults  %d denominator  target=%.1f%%  max_rounds=%d",
        len(_initial_active),
        _pre.get("denominator", 0),
        effective_target,
        effective_max_rounds,
    )

    stats = AtpgStats()
    vectors: list[dict[str, bool]] = []
    seen_patterns: set[str] = set()
    rejected_patterns: dict[int, set[str]] = {}
    candidate_counter = 0

    atpg_seconds = 0.0
    fault_sim_seconds = 0.0
    prev_coverage = 0.0

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

        random_started = time.perf_counter()
        random_batch = core.atpg_random_vectors(
            input_order, cfg.atpg.random_vectors, ATPGRANDOM_SEED
        )
        atpg_seconds += time.perf_counter() - random_started
        new_random: list[dict[str, bool]] = []
        for vector in list(random_batch):
            # LOS random candidates carry head bits = 0; the composite dedup key
            # appends a zero head suffix so it stays length-compatible with LOS
            # blocked keys (V1 ‖ head-bits).
            key = pattern_key(vector, input_order)
            if los:
                key = key + "0" * len(los_head_ports)
            if key in seen_patterns:
                continue
            seen_patterns.add(key)
            new_random.append(vector)
        for vector in new_random:
            stats.generated_vectors += 1
            candidate_counter += 1
            vector_index = len(vectors) + 1
            sim_started = time.perf_counter()
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
                    transition=transition,
                    launch_mode=launch_mode,
                    los_head_scan_in={} if los else None,
                    candidate_key=(
                        pattern_key(vector, input_order) + "0" * len(los_head_ports)
                        if los
                        else None
                    ),
                )
            fault_sim_seconds += time.perf_counter() - sim_started
            if accepted:
                vectors.append(vector)
                stats.accepted_vectors += 1
            else:
                stats.rejected_candidates += 1
                if protocol_no_progress:
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            db_blocked = load_blocked_patterns(conn, campaign_id)

        current_active_ids = {row.fault_id for row in active_rows}
        for row in active_rows:
            fault_id = row.fault_id
            if fault_id not in current_active_ids:
                continue
            blocked = sorted(
                db_blocked.get(fault_id, set()) | rejected_patterns.get(fault_id, set())
            )
            solve_started = time.perf_counter()
            if los:
                solved = dict(
                    core.solve_scan_los_transition_fault_atpg(
                        reduced_json_path,
                        reduced_cell_map,
                        effective_db_path,
                        fault_id,
                        los_couple_ports,
                        los_head_ports,
                        blocked,
                        cfg.atpg.sat_conflict_limit,
                        cfg.atpg.sat_timeout_seconds,
                        unsupported,
                    )
                )
            elif transition:
                solved = dict(
                    core.solve_scan_transition_fault_atpg(
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
            else:
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
            atpg_seconds += time.perf_counter() - solve_started
            result = str(solved["result"])
            if result == "SAT":
                stats.sat += 1
                # Transition SAT returns launch/capture; the launch (V1) is the
                # candidate, and V2 is re-derived inside the candidate processor.
                candidate = dict(solved["launch"] if transition else solved["vector"])
                stats.generated_vectors += 1
                # LOS: capture (V2) carries the SAT-chosen free head scan-in bits;
                # the dedup/blocking key is V1 ‖ head-bits (in head_ports order).
                los_head_scan_in_sat: dict[int, bool] | None = None
                if los:
                    capture = dict(solved["capture"])
                    los_head_scan_in_sat = {
                        ch: bool(capture.get(port, False))
                        for ch, port in los_head_by_chain.items()
                    }
                    key = pattern_key(candidate, input_order) + "".join(
                        "1" if bool(capture.get(port, False)) else "0"
                        for port in los_head_ports
                    )
                else:
                    key = pattern_key(candidate, input_order)
                if key in seen_patterns:
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1
                    continue
                candidate_counter += 1
                vector_index = len(vectors) + 1
                sim_started = time.perf_counter()
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
                        transition=transition,
                        launch_mode=launch_mode,
                        los_head_scan_in=los_head_scan_in_sat,
                        candidate_key=key if los else None,
                    )
                fault_sim_seconds += time.perf_counter() - sim_started
                if accepted:
                    seen_patterns.add(key)
                    vectors.append(candidate)
                    stats.accepted_vectors += 1
                    round_tracker.sat_outcomes.append("SAT")
                    with connect(effective_db_path) as conn:
                        init_schema(conn)
                        current_active_ids = {
                            active.fault_id
                            for active in _active_fault_rows(conn, campaign_id)
                        }
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

        _cov = coverage or 0.0
        log.info(
            "atpg   round %2d/%d  sat=%d unsat=%d timeout=%d"
            "  coverage=%.2f%% (%+.2f%%)  vectors=%d",
            round_idx,
            effective_max_rounds,
            stats.sat,
            stats.unsat,
            stats.timeout,
            _cov,
            _cov - prev_coverage,
            stats.accepted_vectors,
        )
        prev_coverage = _cov

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

    log.info("atpg   terminated: %s", terminal)

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


def _grade_launch_candidate(
    core: Any,
    *,
    scan_ctx: ScanPipelineContext,
    reduced_json_path: str,
    reduced_cell_map: str,
    generic_cell_map: str,
    vector: dict[str, bool],
    input_order: list[str],
    fault_ids: list[int],
    row_by_id: dict[int, _FaultRow],
    generic_site_index: dict[str, int],
    unsupported: str,
    launch_mode: str,
    los_head_scan_in: dict[int, bool] | None,
) -> set[int]:
    """Re-grade one launch candidate (V1) against  via the two-capture
    scan-protocol sim on the generic netlist. Returns the detected subset. This is
    the same authoritative engine the forward pipeline grades with, so it is safe
    for coverage-preserving compaction (unlike a single-frame regrade)."""
    is_los = launch_mode == "los"
    is_loc = launch_mode == "loc"
    head_bits = los_head_scan_in or {}
    materialized_launch = _materialize_reduced_outputs(
        core,
        reduced_json_path,
        reduced_cell_map,
        vector,
        input_order,
        scan_ctx.functional_output_order,
        scan_ctx.pseudo_port_map,
        unsupported,
    )
    if is_los:
        capture_vector = _derive_los_capture(
            vector, head_bits, scan_ctx.pseudo_port_map
        )
    else:
        capture_vector = _derive_loc_capture(
            materialized_launch, vector, scan_ctx.pseudo_port_map
        )
    reduced_expectation = _materialize_reduced_outputs(
        core,
        reduced_json_path,
        reduced_cell_map,
        capture_vector,
        input_order,
        scan_ctx.functional_output_order,
        scan_ctx.pseudo_port_map,
        unsupported,
    )
    serialize_input = dict(vector)
    for entry in scan_ctx.pseudo_port_map.values():
        serialize_input[str(entry["ppo_port"])] = bool(
            reduced_expectation.get(str(entry["ppo_port"]), False)
        )
    scan_pattern = serialize_vector(
        serialize_input, scan_ctx.pseudo_port_map, scan_ctx.manifest
    )

    sim_ids: list[int] = []
    specs: list[tuple[int, int]] = []
    for fault_id in fault_ids:
        row = row_by_id.get(fault_id)
        if row is None:
            continue
        cidx = generic_site_index.get(row.fault_site_key)
        if cidx is None:
            continue
        sim_ids.append(fault_id)
        specs.append((cidx, fault_type_to_sa_code(row.fault_type)))
    if not specs:
        return set()

    kwargs = _protocol_fault_sim_kwargs(scan_ctx, scan_pattern)
    result = dict(
        core.simulate_scan_protocol_faults(
            str(scan_ctx.generic_json),
            generic_cell_map,
            faults=specs,
            unsupported_policy=unsupported,
            loc_two_capture=is_loc,
            los_two_capture=is_los,
            los_launch_scan_in=head_bits,
            **kwargs,
        )
    )
    lanes = [
        dict(lane)
        for batch in result.get("batches", [])
        for lane in batch.get("lanes", [])
    ]
    passed: set[int] = set()
    for fault_id, lane in zip(sim_ids, lanes):
        if str(lane.get("outcome")) == "pass":
            passed.add(fault_id)
    return passed


def compact_run_scan_transition(
    core: Any,
    *,
    scan_ctx: ScanPipelineContext,
    reduced_json_path: str,
    reduced_cell_map: str,
    generic_cell_map: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vectors: VectorSet,
    unsupported: str,
    launch_mode: str,
) -> tuple[VectorSet, int, float]:
    """Reverse-order PROTOCOL-based static compaction of scan transition vectors.

    Each kept launch (V1) is re-graded against the remaining-undetected detected
    set via the two-capture scan-protocol sim, so coverage is preserved exactly.
    Returns (compacted_vectors, new_run_id, raw_count)."""
    from faultflow.runner.compaction import _decode, _insert_compacted_run, _pattern

    input_order = vectors.input_order
    with connect(db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT pattern, launch_pattern
            FROM vectors
            WHERE campaign_id = ? AND run_id = ?
            ORDER BY vector_index
            """,
            (campaign_id, run_id),
        ).fetchall()
        detected_rows = _detected_fault_rows(conn, campaign_id)
    raw_count = len(rows)
    if raw_count == 0:
        return vectors, run_id, float(raw_count)

    row_by_id = {row.fault_id: row for row in detected_rows}
    remaining: set[int] = set(row_by_id)
    generic_site_index = build_site_key_index(
        core, scan_ctx.generic_json, generic_cell_map, unsupported
    )
    _couples, _heads, head_by_chain = build_los_couples(scan_ctx.pseudo_port_map)

    launches: list[dict[str, bool]] = []
    captures: list[dict[str, bool]] = []
    head_bits_list: list[dict[int, bool]] = []
    for row in rows:
        launch = _decode(str(row["launch_pattern"]), input_order)
        capture = _decode(str(row["pattern"]), input_order)
        launches.append(launch)
        captures.append(capture)
        if launch_mode == "los":
            head_bits_list.append(
                {
                    ch: bool(capture.get(port, False))
                    for ch, port in head_by_chain.items()
                }
            )
        else:
            head_bits_list.append({})

    started = time.perf_counter()
    kept_indices: list[int] = []
    for idx in range(raw_count - 1, -1, -1):
        if not remaining:
            break
        detected = _grade_launch_candidate(
            core,
            scan_ctx=scan_ctx,
            reduced_json_path=reduced_json_path,
            reduced_cell_map=reduced_cell_map,
            generic_cell_map=generic_cell_map,
            vector=launches[idx],
            input_order=input_order,
            fault_ids=sorted(remaining),
            row_by_id=row_by_id,
            generic_site_index=generic_site_index,
            unsupported=unsupported,
            launch_mode=launch_mode,
            los_head_scan_in=head_bits_list[idx],
        )
        newly = detected & remaining
        if newly:
            kept_indices.append(idx)
            remaining.difference_update(newly)
    kept_indices.sort()

    source = f"compacted_{vectors.source}"
    capture_patterns = [_pattern(captures[i], input_order) for i in kept_indices]
    launch_patterns = [_pattern(launches[i], input_order) for i in kept_indices]
    with connect(db_path) as conn:
        init_schema(conn)
        with conn:
            new_run_id = _insert_compacted_run(conn, run_id, source, len(kept_indices))
    if capture_patterns:
        core.append_vectors(
            db_path,
            campaign_id,
            new_run_id,
            source,
            capture_patterns,
            1,
            launch_patterns,
        )

    with connect(db_path) as conn:
        init_schema(conn)
        coverage = summary(conn, campaign_id=campaign_id)["coverage_percent"]
    log.info(
        "compact  %d -> %d scan transition pairs  (-%d)  %.2fs  coverage=%.3f%%",
        raw_count,
        len(kept_indices),
        raw_count - len(kept_indices),
        time.perf_counter() - started,
        float(coverage or 0.0),
    )
    kept_captures = [captures[i] for i in kept_indices]
    return VectorSet(source, input_order, kept_captures), new_run_id, float(raw_count)
