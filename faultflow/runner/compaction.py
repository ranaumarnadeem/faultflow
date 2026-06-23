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


# A vector detecting more than this many new faults is dense (few don't-cares,
# rarely merges) — kept fully specified to bound the X-extraction sim cost; only
# sparse patterns (the hard SAT-targeted faults) are X-extracted and packed.
_DYNAMIC_DENSE_FAULTS = 8


def _extract_cube(
    detect, vector: dict[str, bool], input_order: list[str], targets: set[int]
) -> dict[str, bool]:
    """The specified-bit subset of ``vector`` that still detects ``targets``.

    A PI is a don't-care iff ``targets`` stay detected for BOTH its values (other
    bits held at ``vector``); such bits are dropped. Returns specified bits only
    ({name: bool}). 2 sims per PI — called only for sparse patterns.
    """
    spec: dict[str, bool] = {}
    for name in input_order:
        v_lo = dict(vector)
        v_lo[name] = False
        v_hi = dict(vector)
        v_hi[name] = True
        if targets.issubset(detect(v_lo, targets)) and targets.issubset(
            detect(v_hi, targets)
        ):
            continue  # don't-care
        spec[name] = bool(vector.get(name, False))
    return spec


def _cubes_compatible(a: dict[str, bool], b: dict[str, bool]) -> bool:
    """True iff no PI is specified in both cubes with conflicting values."""
    return all(b[name] == a[name] for name in b if name in a)


def compact_run_dynamic(
    *,
    json_path: str,
    cell_map_path: str,
    db_path: str,
    campaign_id: int,
    run_id: int,
    vectors: VectorSet,
    unsupported: str,
) -> tuple[VectorSet, int, int]:
    """Dynamic compaction via sim-verified cube packing.

    Selects the contributing vectors (reverse-order, like ``compact_run``),
    X-extracts each sparse vector's don't-cares, then greedily merges compatible
    cubes — RE-SIMULATING every merge to confirm all member faults stay detected.
    Produces NEW packed patterns, fewer than reverse when don't-cares allow.
    Coverage is preserved by construction (fault status is untouched; every packed
    pattern's members are sim-verified). Falls back to ``compact_run`` if packing
    fails to beat it or yields an invalid set, so dynamic is always <= reverse and
    always correct.
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

    def detect(vector: dict[str, bool], targets: set[int]) -> set[int]:
        return set(
            core.compaction_detections(
                json_path,
                cell_map_path,
                db_path,
                vector,
                input_order,
                sorted(targets),
                unsupported,
            )
        )

    started = time.perf_counter()
    with connect(db_path) as conn:
        init_schema(conn)
        all_detected = set(_detected_fault_ids(conn, campaign_id))

    # 1. Contributing vectors (same selection as reverse), each with the faults it
    #    newly covers.
    remaining = set(all_detected)
    contributions: list[tuple[dict[str, bool], set[int]]] = []
    for idx in range(raw_count - 1, -1, -1):
        if not remaining:
            break
        vector = vectors.vectors[idx]
        newly = detect(vector, remaining) & remaining
        if newly:
            contributions.append((vector, newly))
            remaining.difference_update(newly)

    # 2. Pack: X-extract a cube per sparse contribution, best-fit-merge into an open
    #    packed pattern (re-sim verified). Dense patterns stay fully specified (so
    #    they never merge and stand alone, exactly as reverse would keep them).
    open_cubes: list[dict] = []  # {"spec": {name:bool}, "members": set, "vector": dict}
    for vector, newly in contributions:
        if len(newly) > _DYNAMIC_DENSE_FAULTS:
            spec = {name: bool(vector.get(name, False)) for name in input_order}
        else:
            spec = _extract_cube(detect, vector, input_order, newly)
        placed = False
        for oc in open_cubes:
            if not _cubes_compatible(oc["spec"], spec):
                continue
            trial = dict(oc["vector"])
            trial.update(spec)
            members = oc["members"] | newly
            if members.issubset(detect(trial, members)):
                oc["vector"] = trial
                oc["spec"] = {**oc["spec"], **spec}
                oc["members"] = members
                placed = True
                break
        if not placed:
            # Standalone: the original (fully-specified) vector detects newly.
            open_cubes.append(
                {"spec": spec, "members": set(newly), "vector": dict(vector)}
            )

    packed = [oc["vector"] for oc in open_cubes]

    # 3. Safety net: the packed set must detect every originally-detected fault and
    #    be no larger than reverse; otherwise fall back to the proven reverse result.
    packed_detected: set[int] = set()
    for vector in packed:
        packed_detected |= detect(vector, all_detected)
    if not all_detected.issubset(packed_detected) or len(packed) > len(contributions):
        log.info("compact(dynamic)  packing did not beat reverse; falling back")
        return compact_run(
            json_path=json_path,
            cell_map_path=cell_map_path,
            db_path=db_path,
            campaign_id=campaign_id,
            run_id=run_id,
            vectors=vectors,
            unsupported=unsupported,
        )

    # 4. Write the packed set as a new compacted run (mirrors compact_run).
    source = f"compacted_{vectors.source}"
    patterns = [_pattern(vector, input_order) for vector in packed]
    with connect(db_path) as conn:
        init_schema(conn)
        with conn:
            new_run_id = _insert_compacted_run(conn, run_id, source, len(packed))
    if patterns:
        core.append_vectors(db_path, campaign_id, new_run_id, source, patterns, 1)

    with connect(db_path) as conn:
        init_schema(conn)
        coverage = summary(conn, campaign_id=campaign_id)["coverage_percent"]

    log.info(
        "compact(dynamic)  %d -> %d vectors  (-%d)  %.2fs  coverage=%.3f%%",
        raw_count,
        len(packed),
        raw_count - len(packed),
        time.perf_counter() - started,
        float(coverage or 0.0),
    )
    return VectorSet(source, input_order, packed), new_run_id, raw_count


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
