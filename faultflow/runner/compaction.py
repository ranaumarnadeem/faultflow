from __future__ import annotations

import logging
import sqlite3
import time

from faultflow.atpg import VectorSet
from faultflow.db import connect, init_schema, summary
from faultflow.runner.runner import RunnerError, _load_core

log = logging.getLogger(__name__)


def _pattern(vector: dict[str, bool], input_order: list[str]) -> str:
    return "".join("1" if vector.get(name, False) else "0" for name in input_order)


def _decode(pattern: str, input_order: list[str]) -> dict[str, bool]:
    if len(pattern) != len(input_order):
        raise RunnerError(
            f"vector pattern length {len(pattern)} != {len(input_order)} PIs"
        )
    return {name: pattern[i] == "1" for i, name in enumerate(input_order)}


def _detected_fault_ids(conn: sqlite3.Connection, campaign_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id
        FROM faults
        WHERE campaign_id = ?
          AND status = 'detected'
          AND exclusion = 'none'
          AND collapsed_into IS NULL
        ORDER BY id
        """,
        (campaign_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _insert_compacted_run(
    conn: sqlite3.Connection,
    raw_run_id: int,
    source: str,
    vector_count: int,
) -> int:
    # Copy the ATPG stat columns from the raw run so the report (which reads the
    # latest run by id) shows the real rounds/sat/unsat/terminal/coverage rather
    # than zeros. The timing columns are refreshed afterwards by
    # Runner._write_run_timings against the returned run id.
    conn.execute(
        """
        INSERT INTO runs(
            campaign_id, status, completed_at, vector_source, vector_count,
            initial_ff_state, atpg_generation_seconds, fault_simulation_seconds,
            total_sim_seconds, coverage, atpg_terminal_reason, atpg_rounds,
            atpg_sat, atpg_unsat, atpg_timeout, atpg_unknown,
            atpg_rejected_candidates, atpg_generated_vectors,
            atpg_accepted_vectors, protocol_no_progress_rounds, candidates_aborted)
        SELECT
            campaign_id, 'complete', CURRENT_TIMESTAMP, ?, ?,
            initial_ff_state, atpg_generation_seconds, fault_simulation_seconds,
            total_sim_seconds, coverage, atpg_terminal_reason, atpg_rounds,
            atpg_sat, atpg_unsat, atpg_timeout, atpg_unknown,
            atpg_rejected_candidates, atpg_generated_vectors,
            atpg_accepted_vectors, protocol_no_progress_rounds, candidates_aborted
        FROM runs
        WHERE id = ?
        """,
        (source, vector_count, raw_run_id),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def compact_run(
    *,
    json_path: str,
    cell_map_path: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vectors: VectorSet,
    unsupported: str,
) -> tuple[VectorSet, int, int]:
    """Reverse-order fault-simulation static compaction.

    Produces a coverage-preserving subset of ``vectors`` and writes it as a new
    "compacted" run (the raw run is kept for provenance). Returns
    ``(compacted_vectors, new_run_id, raw_count)``. Coverage is unchanged because
    every fault detected by the full set is still detected by the kept subset.
    """
    raw_count = vectors.count
    input_order = vectors.input_order
    if raw_count == 0:
        return vectors, run_id, raw_count

    core = _load_core()
    if core is None:
        raise RunnerError(
            "C++ extension _faultflow_core is required for compaction. "
            "Run: cmake --build build -- -j2"
        )

    started = time.perf_counter()
    with connect(db_path) as conn:
        init_schema(conn)
        remaining: set[int] = set(_detected_fault_ids(conn, campaign_id))

    kept_indices: list[int] = []
    for idx in range(raw_count - 1, -1, -1):
        if not remaining:
            # Everything is already covered by a later (kept) vector; every
            # earlier vector is redundant.
            break
        detected = core.compaction_detections(
            json_path,
            cell_map_path,
            db_path,
            vectors.vectors[idx],
            input_order,
            sorted(remaining),
            unsupported,
        )
        newly = [fid for fid in detected if fid in remaining]
        if newly:
            kept_indices.append(idx)
            remaining.difference_update(newly)
    kept_indices.sort()
    kept_vectors = [vectors.vectors[i] for i in kept_indices]

    source = f"compacted_{vectors.source}"
    patterns = [_pattern(vector, input_order) for vector in kept_vectors]
    with connect(db_path) as conn:
        init_schema(conn)
        with conn:
            new_run_id = _insert_compacted_run(conn, run_id, source, len(kept_vectors))
    if patterns:
        core.append_vectors(db_path, campaign_id, new_run_id, source, patterns, 1)

    with connect(db_path) as conn:
        init_schema(conn)
        coverage = summary(conn, campaign_id=campaign_id)["coverage_percent"]

    log.info(
        "compact  %d -> %d vectors  (-%d)  %.2fs  coverage=%.3f%%",
        raw_count,
        len(kept_vectors),
        raw_count - len(kept_vectors),
        time.perf_counter() - started,
        float(coverage or 0.0),
    )
    return VectorSet(source, input_order, kept_vectors), new_run_id, raw_count


def compact_run_transition(
    *,
    json_path: str,
    cell_map_path: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vectors: VectorSet,
    unsupported: str,
) -> tuple[VectorSet, int, int]:
    """Reverse-order TWO-FRAME (transition) static compaction.

    Each test is a (launch, capture) pair: capture frames are in ``vectors``,
    launch frames are read from the DB ``vectors.launch_pattern`` column for this
    run. Re-grading uses the qualified two-frame transition sim (the same engine
    that detected the faults), so coverage is preserved. Kept pairs are written to
    a new compacted run with both columns populated.
    """
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
        remaining: set[int] = set(_detected_fault_ids(conn, campaign_id))
    pairs = [
        (
            _decode(str(row["launch_pattern"]), input_order),
            _decode(str(row["pattern"]), input_order),
        )
        for row in rows
    ]
    raw_count = len(pairs)
    if raw_count == 0:
        return vectors, run_id, raw_count

    core = _load_core()
    if core is None:
        raise RunnerError(
            "C++ extension _faultflow_core is required for compaction. "
            "Run: cmake --build build -- -j2"
        )

    started = time.perf_counter()
    kept_indices: list[int] = []
    for idx in range(raw_count - 1, -1, -1):
        if not remaining:
            break
        launch, capture = pairs[idx]
        detected = core.compaction_pair_detections(
            json_path,
            cell_map_path,
            db_path,
            launch,
            capture,
            input_order,
            sorted(remaining),
            unsupported,
        )
        newly = [fid for fid in detected if fid in remaining]
        if newly:
            kept_indices.append(idx)
            remaining.difference_update(newly)
    kept_indices.sort()
    kept_pairs = [pairs[i] for i in kept_indices]

    source = f"compacted_{vectors.source}"
    capture_patterns = [_pattern(capture, input_order) for _l, capture in kept_pairs]
    launch_patterns = [_pattern(launch, input_order) for launch, _c in kept_pairs]
    with connect(db_path) as conn:
        init_schema(conn)
        with conn:
            new_run_id = _insert_compacted_run(conn, run_id, source, len(kept_pairs))
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
        "compact  %d -> %d transition pairs  (-%d)  %.2fs  coverage=%.3f%%",
        raw_count,
        len(kept_pairs),
        raw_count - len(kept_pairs),
        time.perf_counter() - started,
        float(coverage or 0.0),
    )
    kept_captures = [capture for _l, capture in kept_pairs]
    return VectorSet(source, input_order, kept_captures), new_run_id, raw_count
