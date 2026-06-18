from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.atpg import VectorSet
from faultflow.config import FaultflowConfig
from faultflow.db import (
    CAMPAIGN_TYPE_COMB,
    CAMPAIGN_TYPE_SCAN,
    connect,
    init_schema,
    summary,
)
from faultflow.runner.runner import RunnerError

log = logging.getLogger(__name__)

DEFAULT_MAX_ATPG_ROUNDS = 20
ATPGRANDOM_SEED = 0x5EED5EED


@dataclass
class AtpgStats:
    rounds: int = 0
    sat: int = 0
    unsat: int = 0
    timeout: int = 0
    unknown: int = 0
    rejected_candidates: int = 0
    generated_vectors: int = 0
    accepted_vectors: int = 0
    protocol_no_progress_rounds: int = 0
    terminal_reason: str = "COMPLETE"


@dataclass
class _RoundTracker:
    sat_outcomes: list[str] = field(default_factory=list)


def pattern_key(vector: dict[str, bool], input_order: list[str]) -> str:
    return "".join("1" if vector.get(name, False) else "0" for name in input_order)


def redundancy_model_id(fp: dict[str, Any]) -> str:
    return "|".join(
        [
            str(fp["netlist_hash"]),
            str(fp["cell_lib_hash"]),
            str(fp["collapsing"]),
            str(fp["unsupported_cells"]),
            str(fp["include_clock_faults"]),
            str(fp["include_reset_faults"]),
            # Transition UNSAT-redundancy is model-specific: a fault redundant
            # under stuck-at need not be redundant under transition, so the model
            # is part of the redundancy fingerprint.
            str(fp.get("fault_model", "stuck_at")),
        ]
    )


def _active_fault_rows(conn: sqlite3.Connection, campaign_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id
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


def _fault_counts(conn: sqlite3.Connection, campaign_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'detected' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS detected,
          SUM(CASE WHEN status = 'redundant' AND exclusion = 'none'
                    AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS redundant
        FROM faults
        WHERE campaign_id = ?
        """,
        (campaign_id,),
    ).fetchone()
    return int(row["detected"] or 0), int(row["redundant"] or 0)


def _append_unique_vectors(
    vectors: list[dict[str, bool]],
    seen_patterns: set[str],
    input_order: list[str],
    batch: list[dict[str, bool]],
) -> list[dict[str, bool]]:
    accepted: list[dict[str, bool]] = []
    for vector in batch:
        key = pattern_key(vector, input_order)
        if key in seen_patterns:
            continue
        seen_patterns.add(key)
        vectors.append(vector)
        accepted.append(vector)
    return accepted


def _accept_and_simulate(
    core: Any,
    *,
    json_path: str,
    cell_map_path: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vector_source: str,
    vector: dict[str, bool],
    input_order: list[str],
    fault_ids: list[int],
    vector_index: int,
    unsupported: str,
    on_vector_accepted: Callable[[dict[str, bool], int | None], None] | None,
) -> None:
    fault_id = fault_ids[0] if len(fault_ids) == 1 else None
    if on_vector_accepted is not None:
        on_vector_accepted(vector, fault_id)
    key = pattern_key(vector, input_order)
    core.append_vectors(
        db_path, campaign_id, run_id, vector_source, [key], vector_index
    )
    core.simulate_incremental(
        json_path,
        cell_map_path,
        db_path,
        campaign_id,
        run_id,
        [vector],
        input_order,
        fault_ids,
        vector_index,
        unsupported,
    )
    core.update_run_vector_count(db_path, run_id, vector_index)


def transition_pattern_key(
    launch: dict[str, bool], capture: dict[str, bool], input_order: list[str]
) -> str:
    """Combined launch||capture key for deduping transition vector pairs."""
    return pattern_key(launch, input_order) + pattern_key(capture, input_order)


def _append_unique_pairs(
    pairs: list[tuple[dict[str, bool], dict[str, bool]]],
    seen_patterns: set[str],
    input_order: list[str],
    batch: list[tuple[dict[str, bool], dict[str, bool]]],
) -> list[tuple[dict[str, bool], dict[str, bool]]]:
    accepted: list[tuple[dict[str, bool], dict[str, bool]]] = []
    for launch, capture in batch:
        key = transition_pattern_key(launch, capture, input_order)
        if key in seen_patterns:
            continue
        seen_patterns.add(key)
        pairs.append((launch, capture))
        accepted.append((launch, capture))
    return accepted


def _accept_and_simulate_transition(
    core: Any,
    *,
    json_path: str,
    cell_map_path: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vector_source: str,
    launch: dict[str, bool],
    capture: dict[str, bool],
    input_order: list[str],
    fault_ids: list[int],
    vector_index: int,
    unsupported: str,
) -> None:
    launch_key = pattern_key(launch, input_order)
    capture_key = pattern_key(capture, input_order)
    # Capture frame in `pattern`, launch frame in `launch_pattern`.
    core.append_vectors(
        db_path,
        campaign_id,
        run_id,
        vector_source,
        [capture_key],
        vector_index,
        [launch_key],
    )
    core.simulate_transition_incremental(
        json_path,
        cell_map_path,
        db_path,
        campaign_id,
        run_id,
        [(launch, capture)],
        input_order,
        fault_ids,
        vector_index,
        unsupported,
    )
    core.update_run_vector_count(db_path, run_id, vector_index)


def _termination_sweep_q_stems(conn: sqlite3.Connection, campaign_id: int) -> int:
    cur = conn.execute(
        """
        UPDATE faults
        SET protocol_unresolved = 1
        WHERE campaign_id = ?
          AND status = 'undetected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
          AND fault_site_key LIKE 'net:%:stem'
        """,
        (campaign_id,),
    )
    conn.commit()
    return int(cur.rowcount)


def _should_stall(
    *,
    detected: int,
    redundant: int,
    prev_detected: int,
    prev_redundant: int,
    round_outcomes: list[str],
) -> bool:
    if detected != prev_detected or redundant != prev_redundant:
        return False
    if not round_outcomes:
        return True
    allowed = {"TIMEOUT", "UNKNOWN", "protocol_no_progress"}
    return all(outcome in allowed for outcome in round_outcomes)


def run_progressive_native_atpg(
    cfg: FaultflowConfig,
    netlist: Path,
    redundancy_model: str,
    *,
    campaign_id: int,
    max_rounds: int | None = None,
    target_coverage: float | None = None,
    db_path: Path | None = None,
    cell_map_path: Path | None = None,
    vector_source: str = "native_sat_atpg",
    campaign_type: str = CAMPAIGN_TYPE_COMB,
    on_vector_accepted: Callable[[dict[str, bool], int | None], None] | None = None,
    scan_ctx: Any | None = None,
) -> tuple[VectorSet, AtpgStats, int, float, float]:
    from faultflow.runner.runner import _load_core, _port_names

    if campaign_type == CAMPAIGN_TYPE_SCAN:
        if scan_ctx is None:
            raise RunnerError("scan_ctx is required for scan progressive ATPG")
        from faultflow.scan.detection_pipeline import run_progressive_scan_atpg

        return run_progressive_scan_atpg(
            cfg,
            netlist,
            redundancy_model,
            campaign_id=campaign_id,
            scan_ctx=scan_ctx,
            max_rounds=max_rounds,
            target_coverage=target_coverage,
            db_path=db_path,
            cell_map_path=cell_map_path,
            vector_source=vector_source,
        )

    del scan_ctx

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
    json_path = str(netlist)
    effective_cell_map = str(
        cell_map_path if cell_map_path is not None else cfg.cell_lib
    )
    unsupported = cfg.simulation.unsupported_cells

    core.ensure_faults_enumerated(
        json_path,
        effective_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
    )
    core.invalidate_stale_redundant(effective_db_path, campaign_id, redundancy_model)

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
            active_ids = [int(row["id"]) for row in active_rows]

        if not active_ids:
            terminal = "COMPLETE"
            break

        atpg_started = time.perf_counter()
        random_batch = core.atpg_random_vectors(
            input_order, cfg.atpg.random_vectors, ATPGRANDOM_SEED
        )
        atpg_seconds += time.perf_counter() - atpg_started
        new_random = _append_unique_vectors(
            vectors, seen_patterns, input_order, list(random_batch)
        )
        if new_random:
            stats.generated_vectors += len(new_random)
            stats.accepted_vectors += len(new_random)
            base_index = len(vectors) - len(new_random) + 1
            for offset, vector in enumerate(new_random):
                vector_index = base_index + offset
                sim_started = time.perf_counter()
                _accept_and_simulate(
                    core,
                    json_path=json_path,
                    cell_map_path=effective_cell_map,
                    db_path=effective_db_path,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    vector_source=vector_source,
                    vector=vector,
                    input_order=input_order,
                    fault_ids=active_ids,
                    vector_index=vector_index,
                    unsupported=unsupported,
                    on_vector_accepted=on_vector_accepted,
                )
                fault_sim_seconds += time.perf_counter() - sim_started

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            active_ids = [int(row["id"]) for row in active_rows]

        for fault_id in active_ids:
            blocked = sorted(rejected_patterns.get(fault_id, set()))
            atpg_started = time.perf_counter()
            solved = dict(
                core.solve_fault_atpg(
                    json_path,
                    effective_cell_map,
                    effective_db_path,
                    fault_id,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    cfg.atpg.sat_timeout_seconds,
                    unsupported,
                )
            )
            atpg_seconds += time.perf_counter() - atpg_started
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
                sim_started = time.perf_counter()
                verified = core.verify_fault_candidate(
                    json_path,
                    effective_cell_map,
                    effective_db_path,
                    fault_id,
                    candidate,
                    unsupported,
                )
                fault_sim_seconds += time.perf_counter() - sim_started
                if verified:
                    seen_patterns.add(key)
                    vectors.append(candidate)
                    stats.accepted_vectors += 1
                    vector_index = len(vectors)
                    sim_started = time.perf_counter()
                    _accept_and_simulate(
                        core,
                        json_path=json_path,
                        cell_map_path=effective_cell_map,
                        db_path=effective_db_path,
                        campaign_id=campaign_id,
                        run_id=run_id,
                        vector_source=vector_source,
                        vector=candidate,
                        input_order=input_order,
                        fault_ids=[fault_id],
                        vector_index=vector_index,
                        unsupported=unsupported,
                        on_vector_accepted=on_vector_accepted,
                    )
                    fault_sim_seconds += time.perf_counter() - sim_started
                    round_tracker.sat_outcomes.append("SAT")
                else:
                    stats.rejected_candidates += 1
                    rejected_patterns.setdefault(fault_id, set()).add(key)
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1
            elif result == "UNSAT":
                stats.unsat += 1
                core.mark_fault_redundant(effective_db_path, fault_id, redundancy_model)
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
                _termination_sweep_q_stems(conn, campaign_id)
            terminal = "STALLED"
            break

    log.info("atpg   terminated: %s", terminal)

    with connect(effective_db_path) as conn:
        init_schema(conn)
        if terminal == "MAX_ROUNDS":
            _termination_sweep_q_stems(conn, campaign_id)
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


def run_progressive_transition_atpg(
    cfg: FaultflowConfig,
    netlist: Path,
    redundancy_model: str,
    *,
    campaign_id: int,
    max_rounds: int | None = None,
    target_coverage: float | None = None,
    db_path: Path | None = None,
    cell_map_path: Path | None = None,
    vector_source: str = "native_transition_atpg",
) -> tuple[VectorSet, AtpgStats, int, float, float]:
    """Combinational (broadside two-pattern) transition-fault progressive ATPG.

    Mirrors run_progressive_native_atpg but every vector is a launch/capture
    pair: STR/STF reuse the SA0/SA1 fault rows, solved/verified/simulated through
    the two-frame core entry points. The returned VectorSet holds capture frames;
    launch frames are persisted in the vectors.launch_pattern column.
    """
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
    json_path = str(netlist)
    effective_cell_map = str(
        cell_map_path if cell_map_path is not None else cfg.cell_lib
    )
    unsupported = cfg.simulation.unsupported_cells

    # Shared enumeration: STR/STF reuse the SA0/SA1 fault rows.
    core.ensure_faults_enumerated(
        json_path,
        effective_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
    )
    core.invalidate_stale_redundant(effective_db_path, campaign_id, redundancy_model)

    with connect(effective_db_path) as conn:
        init_schema(conn)
        _pre = summary(conn, campaign_id=campaign_id)
        _initial_active = _active_fault_rows(conn, campaign_id)
    log.info(
        "atpg   transition: %d active faults  %d denominator  target=%.1f%%"
        "  max_rounds=%d",
        len(_initial_active),
        _pre.get("denominator", 0),
        effective_target,
        effective_max_rounds,
    )

    stats = AtpgStats()
    pairs: list[tuple[dict[str, bool], dict[str, bool]]] = []
    seen_patterns: set[str] = set()
    rejected_patterns: dict[int, set[str]] = {}

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
            active_ids = [int(row["id"]) for row in active_rows]

        if not active_ids:
            terminal = "COMPLETE"
            break

        atpg_started = time.perf_counter()
        random_batch = core.atpg_random_vector_pairs(
            input_order, cfg.atpg.random_vectors, ATPGRANDOM_SEED
        )
        atpg_seconds += time.perf_counter() - atpg_started
        new_random = _append_unique_pairs(
            pairs,
            seen_patterns,
            input_order,
            [(dict(launch), dict(capture)) for launch, capture in random_batch],
        )
        if new_random:
            stats.generated_vectors += len(new_random)
            stats.accepted_vectors += len(new_random)
            base_index = len(pairs) - len(new_random) + 1
            for offset, (launch, capture) in enumerate(new_random):
                vector_index = base_index + offset
                sim_started = time.perf_counter()
                _accept_and_simulate_transition(
                    core,
                    json_path=json_path,
                    cell_map_path=effective_cell_map,
                    db_path=effective_db_path,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    vector_source=vector_source,
                    launch=launch,
                    capture=capture,
                    input_order=input_order,
                    fault_ids=active_ids,
                    vector_index=vector_index,
                    unsupported=unsupported,
                )
                fault_sim_seconds += time.perf_counter() - sim_started

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            active_ids = [int(row["id"]) for row in active_rows]

        for fault_id in active_ids:
            blocked = sorted(rejected_patterns.get(fault_id, set()))
            atpg_started = time.perf_counter()
            solved = dict(
                core.solve_transition_fault_atpg(
                    json_path,
                    effective_cell_map,
                    effective_db_path,
                    fault_id,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    cfg.atpg.sat_timeout_seconds,
                    unsupported,
                )
            )
            atpg_seconds += time.perf_counter() - atpg_started
            result = str(solved["result"])
            if result == "SAT":
                stats.sat += 1
                launch = dict(solved["launch"])
                capture = dict(solved["capture"])
                stats.generated_vectors += 1
                key = transition_pattern_key(launch, capture, input_order)
                if key in seen_patterns:
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1
                    continue
                sim_started = time.perf_counter()
                verified = core.verify_transition_candidate(
                    json_path,
                    effective_cell_map,
                    effective_db_path,
                    fault_id,
                    launch,
                    capture,
                    unsupported,
                )
                fault_sim_seconds += time.perf_counter() - sim_started
                if verified:
                    seen_patterns.add(key)
                    pairs.append((launch, capture))
                    stats.accepted_vectors += 1
                    vector_index = len(pairs)
                    sim_started = time.perf_counter()
                    _accept_and_simulate_transition(
                        core,
                        json_path=json_path,
                        cell_map_path=effective_cell_map,
                        db_path=effective_db_path,
                        campaign_id=campaign_id,
                        run_id=run_id,
                        vector_source=vector_source,
                        launch=launch,
                        capture=capture,
                        input_order=input_order,
                        fault_ids=[fault_id],
                        vector_index=vector_index,
                        unsupported=unsupported,
                    )
                    fault_sim_seconds += time.perf_counter() - sim_started
                    round_tracker.sat_outcomes.append("SAT")
                else:
                    stats.rejected_candidates += 1
                    rejected_patterns.setdefault(fault_id, set()).add(key)
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1
            elif result == "UNSAT":
                stats.unsat += 1
                core.mark_fault_redundant(effective_db_path, fault_id, redundancy_model)
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
            "atpg   transition round %2d/%d  sat=%d unsat=%d timeout=%d"
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
                _termination_sweep_q_stems(conn, campaign_id)
            terminal = "STALLED"
            break

    log.info("atpg   transition terminated: %s", terminal)

    with connect(effective_db_path) as conn:
        init_schema(conn)
        if terminal == "MAX_ROUNDS":
            _termination_sweep_q_stems(conn, campaign_id)
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
    captures = [capture for _launch, capture in pairs]
    return (
        VectorSet(vector_source, input_order, captures),
        stats,
        run_id,
        atpg_seconds,
        fault_sim_seconds,
    )
