from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from faultflow.atpg import VectorSet
from faultflow.config import (
    FaultflowConfig,
    interleave_easy_hard,
    parse_timeout_schedule,
    resolve_sim_threads,
)
from faultflow.db import (
    CAMPAIGN_TYPE_COMB,
    CAMPAIGN_TYPE_SCAN,
    connect,
    init_schema,
    record_reconvergent_stems,
    record_sat_outcomes,
    summary,
)
from faultflow.runner.parallel_solve import solve_fault_worker
from faultflow.runner.runner import RunnerError
from faultflow.testpoint.preflight import PreflightData, run_preflight

log = logging.getLogger(__name__)

# Heartbeat interval (seconds) for intra-round grading progress. A round can grade
# hundreds of vectors over many minutes; without this the run looks frozen and you
# can only guess from CPU/RAM whether it is still working.
GRADE_PROGRESS_INTERVAL = 15.0


def _live_detected(db_path: str, campaign_id: int) -> int:
    """Current detected-fault count for the campaign (for the live heartbeat)."""
    with connect(db_path) as conn:
        init_schema(conn)
        detected, _ = _fault_counts(conn, campaign_id)
    return detected


class GradeHeartbeat:
    """Throttled INFO heartbeat for a round's vector-grading phase.

    Emits at most one line per `interval` seconds showing how many vectors have
    been graded and the LIVE detected-fault count with its delta since the last
    tick — so a long ATPG round visibly makes progress (or visibly stalls) rather
    than running silent until the round-end summary. On by default (INFO).
    """

    def __init__(
        self,
        logger: logging.Logger,
        round_idx: int,
        total: int,
        detected_fn: Callable[[], int],
        interval: float = GRADE_PROGRESS_INTERVAL,
    ) -> None:
        self._logger = logger
        self._round = round_idx
        self._total = total
        self._detected_fn = detected_fn
        self._interval = interval
        now = time.perf_counter()
        self._start = now
        self._last = now
        self._prev: int | None = None

    def tick(self, done: int) -> None:
        now = time.perf_counter()
        if now - self._last < self._interval:
            return
        self._last = now
        detected = self._detected_fn()
        delta = 0 if self._prev is None else detected - self._prev
        self._prev = detected
        self._logger.info(
            "atpg   round %2d  graded %d/%d vectors  detected=%d (+%d)  elapsed=%.0fs",
            self._round,
            done,
            self._total,
            detected,
            delta,
            now - self._start,
        )


class SolveHeartbeat:
    """Throttled INFO heartbeat for a round's SAT-solve phase.

    The parallel wave (``executor.map``) blocks while CaDiCaL solves up to a few
    hundred faults — silent for many minutes, indistinguishable from a hang.
    Emits at most one line per `interval` seconds with faults solved so far, the
    LIVE detected count and its delta, and a rough ETA (linear extrapolation from
    the current solve rate). Mirrors GradeHeartbeat; on by default (INFO).
    """

    def __init__(
        self,
        logger: logging.Logger,
        round_idx: int,
        total: int,
        detected_fn: Callable[[], int],
        interval: float = GRADE_PROGRESS_INTERVAL,
    ) -> None:
        self._logger = logger
        self._round = round_idx
        self._total = total
        self._detected_fn = detected_fn
        self._interval = interval
        now = time.perf_counter()
        self._start = now
        self._last = now
        self._prev: int | None = None

    def tick(self, done: int) -> None:
        now = time.perf_counter()
        if now - self._last < self._interval:
            return
        self._last = now
        detected = self._detected_fn()
        delta = 0 if self._prev is None else detected - self._prev
        self._prev = detected
        elapsed = now - self._start
        eta = (elapsed / done) * (self._total - done) if done > 0 else 0.0
        self._logger.info(
            "atpg   round %2d  solved %d/%d faults  detected=%d (+%d)  "
            "elapsed=%.0fs  ~ETA=%.0fs",
            self._round,
            done,
            self._total,
            detected,
            delta,
            elapsed,
            eta,
        )


DEFAULT_MAX_ATPG_ROUNDS = 20
ATPGRANDOM_SEED = 0x5EED5EED
# Parallel SAT is dispatched in waves of (workers * this) survivors instead of
# pre-solving every active fault up front: a pattern accepted in an earlier wave
# fault-drops later faults, so they never reach the solver (saves SAT work on
# wide circuits). Larger = better worker utilization but less drop benefit.
SAT_WAVE_FACTOR = 8


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
            # Launch mode too: an LOC-redundant fault may be LOS-testable (the
            # extra free scan-in head bit), so loc/los redundancy must not alias.
            str(fp.get("launch", "loc")),
            # Blackbox changes the graph (pseudo-PI/TP boundary), so UNSAT-redundant
            # classifications from one blackbox config must not survive to another.
            str(sorted(fp.get("blackbox_instances", []))),
            # IEEE 1500 mode + WBR model change the control/observe boundary, so a
            # fault redundant in FUNCTIONAL/buffer may be testable in INTEST/scan.
            str(fp.get("test_mode", "functional")),
            str(fp.get("wbr_model", "buffer")),
        ]
    )


def _active_fault_rows(conn: sqlite3.Connection, campaign_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id,
               COALESCE(atpg_compiled_net_index, compiled_net_index) AS net_index
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


def _cone_size_order(
    core: Any,
    json_path: str,
    cell_map: str,
    rows: list[sqlite3.Row],
    unsupported: str,
    blackbox_instances: list[str],
) -> dict[int, int]:
    """Structural cone size per fault id, for smallest-cone-first ordering.

    One batched C++ call over the shared cached graph. The size is purely
    structural, so it is computed once and reused across rounds.
    """
    sizes = core.compute_fault_cone_sizes(
        json_path,
        cell_map,
        [int(row["id"]) for row in rows],
        [int(row["net_index"]) for row in rows],
        unsupported,
        blackbox_instances,
    )
    return {int(fault_id): int(size) for fault_id, size in sizes.items()}


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
    blackbox_instances: list[str],
    test_mode: str = "",
    sim_threads: int = 1,
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
        blackbox_instances,
        test_mode,
        sim_threads,
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
    blackbox_instances: list[str],
    sim_threads: int = 1,
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
        blackbox_instances,
        sim_threads,
    )
    core.update_run_vector_count(db_path, run_id, vector_index)


def _precertify_redundant(
    db_path: str, fault_ids: "frozenset[int] | set[int]", redundancy_model_id: str
) -> int:
    """Preflight Phase B: mark canceling-path faults redundant in ONE guarded,
    batched transaction.

    Only active faults are eligible: a fault the simulator already DETECTED is
    empirical ground truth (the structural claim lost), and excluded/collapsed
    faults sit outside the denominator -- Phase B's id set is derived from net
    ids over ALL campaign faults, so the guards are load-bearing, not
    defensive. One connection + one transaction replaces the previous fresh
    autocommit connection per fault (~51 ms/fault on /mnt/c)."""
    if not fault_ids:
        return 0
    conn = connect(db_path)
    try:
        cur = conn.executemany(
            """
            UPDATE faults
            SET status = 'redundant', redundancy_model_id = ?,
                detected_by_vector = NULL
            WHERE id = ?
              AND status = 'undetected'
              AND exclusion = 'none'
              AND collapsed_into IS NULL
            """,
            [(redundancy_model_id, int(fid)) for fid in sorted(fault_ids)],
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


# NOTE: no termination sweep here. protocol_unresolved is a scan-protocol
# concept; the scan pipeline marks its own q-stem faults with a site-key-scoped
# sweep (faultflow/scan/detection_pipeline.py::_termination_sweep_q_stems).
# A previous blanket sweep in this file stamped EVERY undetected stem fault on
# STALLED/MAX_ROUNDS, overwriting the true timeout/unknown reason in the
# coverage report and permanently hiding those faults from every grading path
# that passes skip_protocol_unresolved=true (so resume could not re-target
# them).


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


def _escalation_headroom(
    active_ids: list[int],
    prior_timeout_count: dict[int, int],
    timeout_tiers: list[int],
) -> bool:
    """True while a still-active fault has an untried, longer SAT-timeout tier.

    This holds off the stall verdict: a fault that keeps timing out should be
    retried at every tier of sat_timeout_schedule before the loop gives up,
    otherwise the longest tiers -- the whole point of the schedule -- would never
    be reached once a round stops making other progress.

    prior_timeout_count[fault] is how many times the fault has already timed out,
    which equals how many distinct tiers it has already been attempted at. A fault
    has headroom only if it has actually timed out at least once (count >= 1) AND
    has a longer tier still untried (count < number of tiers). Faults that have not
    timed out -- e.g. protocol-sim failures -- carry no headroom and must not
    suppress a stall. With no schedule (a single tier) headroom is always False, so
    the stall behaviour is exactly as before.
    """
    tier_count = len(timeout_tiers)
    return any(
        0 < prior_timeout_count.get(fault_id, 0) < tier_count for fault_id in active_ids
    )


def _tie_xz_netlist(src: Path, dst: Path) -> int:
    """Replace all 'x'/'z' constant bits with 0 in a Yosys JSON netlist.

    Fixes bits in ports, cell connections, and netnames (all three places
    where Yosys JSON can carry x/z literals).  Returns the number tied.
    """
    netlist = json.loads(src.read_text())
    count = 0

    def fix_bits(bits: list) -> list:
        nonlocal count
        out = []
        for b in bits:
            if b in ("x", "z"):
                out.append(0)
                count += 1
            else:
                out.append(b)
        return out

    for mod in netlist.get("modules", {}).values():
        for port in mod.get("ports", {}).values():
            port["bits"] = fix_bits(port.get("bits", []))
        for cell in mod.get("cells", {}).values():
            for pin in cell.get("connections", {}):
                cell["connections"][pin] = fix_bits(cell["connections"][pin])
        for nn in mod.get("netnames", {}).values():
            nn["bits"] = fix_bits(nn.get("bits", []))

    dst.write_text(json.dumps(netlist, indent=2))
    return count


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
    scan_pattern_out: Path | None = None,
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
            transition=cfg.fault_model.model == "transition",
            launch_mode=cfg.fault_model.launch,
            scan_pattern_out=scan_pattern_out,
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

    _tmpdir: tempfile.TemporaryDirectory | None = None
    if cfg.simulation.tie_xz:
        _tmpdir = tempfile.TemporaryDirectory(prefix="faultflow_tiexz_")
        _tied_path = Path(_tmpdir.name) / netlist.name
        _n = _tie_xz_netlist(netlist, _tied_path)
        if _n:
            log.info("tie_xz: tied %d x/z bits to 0 in %s", _n, netlist.name)
        json_path = str(_tied_path)
    else:
        json_path = str(netlist)

    effective_cell_map = str(
        cell_map_path if cell_map_path is not None else cfg.cell_lib
    )
    unsupported = cfg.simulation.unsupported_cells

    bb_instances = list(cfg.blackbox_instances)
    test_mode = cfg.test_mode
    sim_threads = resolve_sim_threads(cfg.simulation.sim_threads)
    if sim_threads > 1:
        log.info("parallel fault grading across %d threads", sim_threads)
    core.ensure_faults_enumerated(
        json_path,
        effective_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
        bb_instances,
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

    # Escalating SAT timeout (see parse_timeout_schedule): each fault starts at
    # the smallest tier and only escalates after it times out. prior_timeout_count
    # persists across rounds so a repeatedly-timing-out fault gets more budget.
    timeout_tiers = parse_timeout_schedule(
        cfg.atpg.sat_timeout_schedule, cfg.atpg.sat_timeout_seconds
    )
    prior_timeout_count: dict[int, int] = {}
    if len(timeout_tiers) > effective_max_rounds:
        log.warning(
            "atpg   sat_timeout_schedule has %d tiers but only %d rounds are "
            "allowed; the longest tiers will never be reached",
            len(timeout_tiers),
            effective_max_rounds,
        )

    # Cone-size fault ordering (atpg.order_by_cone_size): structural cone size is
    # computed lazily per wave (W faults at a time) so faults that are detected by
    # pattern grading never pay the BFS cost.
    order_faults = cfg.atpg.order_by_cone_size

    # Parallel SAT solving — same fork+copy-on-write scheme as the scan pipeline.
    _parallel = cfg.atpg.workers > 1
    _executor: ProcessPoolExecutor | None = None
    if _parallel:
        log.info(
            "atpg   warming graph cache before forking %d worker processes",
            cfg.atpg.workers,
        )
        core.compute_fault_cone_sizes(
            json_path, effective_cell_map, [], [], unsupported
        )
        try:
            _executor = ProcessPoolExecutor(
                max_workers=cfg.atpg.workers,
                mp_context=get_context("fork"),
            )
        except Exception as _fork_exc:
            log.warning(
                "atpg   fork-based worker pool unavailable (%s); serial fallback",
                _fork_exc,
            )
            _parallel = False

    # OT structural reconvergence preflight (cfg.atpg.preflight).
    # Phase A: reconvergent-site faults sorted last + tier bumped to skip the
    # short timeout tier.
    # Phase B: canceling-path stems marked UNSAT directly (no SAT call needed).
    # Falls back silently if opentest is not on PATH.
    _preflight: PreflightData | None = None
    _reconv_fault_ids: frozenset[int] = frozenset()
    if cfg.atpg.preflight:
        _opentest_bin = shutil.which("opentest") or shutil.which("opentest-cli")
        if _opentest_bin:
            _lib = str(cfg.cell_lib).lower()
            _tech = cfg.atpg.preflight_tech or (
                "osu035" if ("osu035" in _lib or "osu" in _lib) else "sky130"
            )
            _preflight = run_preflight(
                Path(json_path),
                Path(json_path).parent / "preflight",
                _opentest_bin,
                _tech,
            )
            if _preflight:
                # Build fault_id → net_id mapping from DB (one query, reused below).
                with connect(effective_db_path) as conn:
                    _fid_to_netid: dict[int, int] = {
                        int(r["id"]): int(r["net_id"])
                        for r in conn.execute(
                            "SELECT id, net_id FROM faults WHERE campaign_id = ?",
                            (campaign_id,),
                        ).fetchall()
                    }
                    # Persist the pre-computed reconvergent stems so the coverage
                    # report can name them as bottleneck_net (one write/campaign).
                    init_schema(conn)
                    record_reconvergent_stems(
                        conn, campaign_id, _preflight.fanout_yosys_ids
                    )
                    conn.commit()
                _reconv_fault_ids = frozenset(
                    fid
                    for fid, nid in _fid_to_netid.items()
                    if nid in _preflight.fanout_yosys_ids
                )
                _redundant_fault_ids = frozenset(
                    fid
                    for fid, nid in _fid_to_netid.items()
                    if nid in _preflight.redundant_stem_ids
                )
                log.info(
                    "atpg   preflight: %d reconvergent stems, %d canceling stems",
                    len(_preflight.fanout_yosys_ids),
                    len(_preflight.redundant_stem_ids),
                )
                # Phase B: pre-certify canceling-path faults as UNSAT -- one
                # guarded batched txn (never touches detected/excluded/collapsed
                # faults; see _precertify_redundant).
                if _redundant_fault_ids:
                    _b_count = _precertify_redundant(
                        effective_db_path, _redundant_fault_ids, redundancy_model
                    )
                    if _b_count:
                        log.info(
                            "atpg   preflight Phase B: pre-certified %d faults"
                            " as UNSAT",
                            _b_count,
                        )

    _easy_reserve = cfg.atpg.easy_fault_reserve
    if _parallel and _easy_reserve > 0 and cfg.atpg.workers >= 4:
        log.info(
            "atpg   easy/hard split: %d easy slots + %d hard slots per wave "
            "(workers=%d, easy_fault_reserve=%d)",
            _easy_reserve,
            cfg.atpg.workers - _easy_reserve,
            cfg.atpg.workers,
            _easy_reserve,
        )

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
            heartbeat = GradeHeartbeat(
                log,
                round_idx,
                len(new_random),
                lambda: _live_detected(effective_db_path, campaign_id),
            )
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
                    blackbox_instances=bb_instances,
                    test_mode=test_mode,
                    sim_threads=sim_threads,
                    on_vector_accepted=on_vector_accepted,
                )
                fault_sim_seconds += time.perf_counter() - sim_started
                heartbeat.tick(offset + 1)

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            active_ids = [int(row["id"]) for row in active_rows]

        # O(N) dict for per-wave row lookup without re-scanning active_rows.
        row_by_id: dict[int, Any] = {int(r["id"]): r for r in active_rows}

        # Phase A: seed tier-skip for reconvergent-site faults before sort and
        # before parallel dispatch so both paths pick up the updated count.
        if _reconv_fault_ids:
            for _fid in active_ids:
                if _fid in _reconv_fault_ids:
                    prior_timeout_count[_fid] = max(1, prior_timeout_count.get(_fid, 0))

        # Sort: reconvergent-site faults last (no cone BFS cost on the full set).
        # Cone-size ordering within each group is applied lazily per dispatch wave.
        if _reconv_fault_ids and active_ids:
            active_ids = sorted(
                active_ids,
                key=lambda fid: fid in _reconv_fault_ids,
            )

        # M1: grade each accepted SAT pattern against every still-undetected
        # fault this round (fortuitous-detection dropping), then skip the faults
        # a prior pattern already covered instead of re-solving them. Mirrors the
        # random branch (fault_ids=active_ids) and the scan path.
        drop_sat = cfg.atpg.fault_drop_sat
        remaining = set(active_ids)

        # Parallel SAT is dispatched lazily in WAVES of survivors rather than
        # pre-solving every active fault up front. As the processing loop advances
        # in active_ids order (easy→hard, the deterministic accept order), the next
        # wave of up-to wave_size still-undetected faults is solved in parallel just
        # before it is needed. A pattern accepted earlier fault-drops later faults,
        # which are then excluded from their wave and never reach the solver.
        # workers==1 leaves _parallel_results empty and each survivor is solved
        # serially in the body below (already optimal).
        _parallel_results: dict[int, tuple[str, dict]] = {}
        wave_size = max(cfg.atpg.workers, 1) * SAT_WAVE_FACTOR
        _wave_pos = 0  # next index into active_ids not yet dispatched to a wave
        # Heartbeat for the otherwise-silent SAT-solve phase (parallel wave +
        # serial fallback). Counts faults whose verdict has come back this round.
        _solve_hb = SolveHeartbeat(
            log,
            round_idx,
            len(active_ids),
            lambda: _live_detected(effective_db_path, campaign_id),
        )
        _solved_count = 0
        # Per-fault SAT outcome (timeout/unknown) for this round's reason report,
        # batch-written at round end (one txn) rather than per fault.
        _sat_outcomes: dict[int, str] = {}

        for fault_id in active_ids:
            if drop_sat and fault_id not in remaining:
                continue
            if (
                _parallel
                and _executor is not None
                and fault_id not in _parallel_results
            ):
                # Build the next wave from survivors only, in active_ids order.
                # Interleave easy/hard within the wave for executor balance
                # (results are keyed by fault_id, so dispatch order is immaterial).
                _wave_ids: list[int] = []
                while _wave_pos < len(active_ids) and len(_wave_ids) < wave_size:
                    _wfid = active_ids[_wave_pos]
                    _wave_pos += 1
                    if (not drop_sat) or (_wfid in remaining):
                        _wave_ids.append(_wfid)
                if _wave_ids:
                    if order_faults:
                        _wave_rows = [row_by_id[_wid] for _wid in _wave_ids]
                        _wave_cone_sizes = _cone_size_order(
                            core,
                            json_path,
                            effective_cell_map,
                            _wave_rows,
                            unsupported,
                            bb_instances,
                        )
                        _wave_ids.sort(
                            key=lambda _wid: _wave_cone_sizes.get(_wid, 1 << 30)
                        )
                    _dispatch_ids = (
                        interleave_easy_hard(_wave_ids, cfg.atpg.workers, _easy_reserve)
                        if _easy_reserve > 0 and cfg.atpg.workers >= 4
                        else _wave_ids
                    )
                    _wave_args: list[Any] = [
                        (
                            "native_stuck_at",
                            json_path,
                            effective_cell_map,
                            effective_db_path,
                            fid,
                            sorted(rejected_patterns.get(fid, set())),
                            cfg.atpg.sat_conflict_limit,
                            timeout_tiers[
                                min(
                                    prior_timeout_count.get(fid, 0),
                                    len(timeout_tiers) - 1,
                                )
                            ],
                            unsupported,
                            cfg.atpg.cone_restrict,
                            [],  # los_couple_ports: native path is stuck-at only
                            [],  # los_head_ports
                            list(bb_instances),
                            test_mode,
                            cfg.atpg.incremental_sat,
                        )
                        for fid in _dispatch_ids
                    ]
                    _wave_started = time.perf_counter()
                    try:
                        for _fid, _res, _slv in _executor.map(
                            solve_fault_worker, _wave_args
                        ):
                            _parallel_results[int(_fid)] = (_res, dict(_slv))
                            _solved_count += 1
                            _solve_hb.tick(_solved_count)
                    except BrokenProcessPool as _exc:
                        # A worker process DIED (typically OOM-killed). The pool
                        # is permanently broken: swallowing this used to turn
                        # every remaining fault of every remaining round into a
                        # silent UNKNOWN and "complete" with garbage coverage.
                        # Fail loudly with the remedy instead; DB state written
                        # so far (detections, vectors) is preserved.
                        _executor.shutdown(wait=False, cancel_futures=True)
                        raise RunnerError(
                            "parallel SAT worker process died mid-wave (likely "
                            "out-of-memory). Progress so far is saved; re-run "
                            "with fewer workers (atpg.workers) and/or "
                            "atpg.incremental_sat=false to cut per-worker "
                            "memory."
                        ) from _exc
                    except Exception as _exc:
                        log.warning(
                            "atpg   parallel wave error (%s); faults absent from "
                            "results will be treated as UNKNOWN",
                            _exc,
                        )
                    atpg_seconds += time.perf_counter() - _wave_started
            blocked = sorted(rejected_patterns.get(fault_id, set()))
            tier_timeout = timeout_tiers[
                min(prior_timeout_count.get(fault_id, 0), len(timeout_tiers) - 1)
            ]
            if _parallel_results:
                _pre_res, _pre_slv = _parallel_results.get(
                    fault_id, ("UNKNOWN", {"result": "UNKNOWN"})
                )
                solved = dict(_pre_slv)
                result = _pre_res
            else:
                atpg_started = time.perf_counter()
                solved = dict(
                    core.solve_fault_atpg(
                        json_path,
                        effective_cell_map,
                        effective_db_path,
                        fault_id,
                        blocked,
                        cfg.atpg.sat_conflict_limit,
                        tier_timeout,
                        unsupported,
                        bb_instances,
                        test_mode,
                        cfg.atpg.cone_restrict,
                        cfg.atpg.incremental_sat,
                    )
                )
                atpg_seconds += time.perf_counter() - atpg_started
                result = str(solved["result"])
                _solved_count += 1
                _solve_hb.tick(_solved_count)
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
                    bb_instances,
                    test_mode,
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
                        fault_ids=sorted(remaining) if drop_sat else [fault_id],
                        vector_index=vector_index,
                        unsupported=unsupported,
                        blackbox_instances=bb_instances,
                        test_mode=test_mode,
                        sim_threads=sim_threads,
                        on_vector_accepted=on_vector_accepted,
                    )
                    fault_sim_seconds += time.perf_counter() - sim_started
                    if drop_sat:
                        with connect(effective_db_path) as conn:
                            init_schema(conn)
                            remaining.intersection_update(
                                int(r["id"])
                                for r in _active_fault_rows(conn, campaign_id)
                            )
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
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1
                _sat_outcomes[fault_id] = "timeout"
            else:
                stats.unknown += 1
                round_tracker.sat_outcomes.append("UNKNOWN")
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1
                _sat_outcomes[fault_id] = "unknown"

        with connect(effective_db_path) as conn:
            init_schema(conn)
            record_sat_outcomes(conn, campaign_id, _sat_outcomes, round_idx)
            conn.commit()
            data = summary(conn, campaign_id=campaign_id)
            detected, redundant = _fault_counts(conn, campaign_id)
            active_rows_end = _active_fault_rows(conn, campaign_id)
            active_remaining = len(active_rows_end)
            active_ids_end = [int(row["id"]) for row in active_rows_end]
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
        ) and not _escalation_headroom(
            active_ids_end, prior_timeout_count, timeout_tiers
        ):
            terminal = "STALLED"
            break

    if _executor is not None:
        _executor.shutdown(wait=False)

    log.info("atpg   terminated: %s", terminal)

    with connect(effective_db_path) as conn:
        init_schema(conn)
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

    _tmpdir2: tempfile.TemporaryDirectory | None = None
    if cfg.simulation.tie_xz:
        _tmpdir2 = tempfile.TemporaryDirectory(prefix="faultflow_tiexz_")
        _tied_path2 = Path(_tmpdir2.name) / netlist.name
        _n2 = _tie_xz_netlist(netlist, _tied_path2)
        if _n2:
            log.info("tie_xz: tied %d x/z bits to 0 in %s", _n2, netlist.name)
        json_path = str(_tied_path2)
    else:
        json_path = str(netlist)

    effective_cell_map = str(
        cell_map_path if cell_map_path is not None else cfg.cell_lib
    )
    unsupported = cfg.simulation.unsupported_cells

    bb_instances = list(cfg.blackbox_instances)
    sim_threads = resolve_sim_threads(cfg.simulation.sim_threads)
    if sim_threads > 1:
        log.info("parallel fault grading across %d threads", sim_threads)
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
        bb_instances,
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

    # Escalating SAT timeout (see parse_timeout_schedule): each fault starts at
    # the smallest tier and only escalates after it times out. prior_timeout_count
    # persists across rounds so a repeatedly-timing-out fault gets more budget.
    timeout_tiers = parse_timeout_schedule(
        cfg.atpg.sat_timeout_schedule, cfg.atpg.sat_timeout_seconds
    )
    prior_timeout_count: dict[int, int] = {}
    if len(timeout_tiers) > effective_max_rounds:
        log.warning(
            "atpg   sat_timeout_schedule has %d tiers but only %d rounds are "
            "allowed; the longest tiers will never be reached",
            len(timeout_tiers),
            effective_max_rounds,
        )

    if cfg.atpg.order_by_cone_size:
        log.info(
            "atpg   cone-size ordering not applied to transition faults; "
            "using enumeration order"
        )

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
            heartbeat = GradeHeartbeat(
                log,
                round_idx,
                len(new_random),
                lambda: _live_detected(effective_db_path, campaign_id),
            )
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
                    blackbox_instances=bb_instances,
                    sim_threads=sim_threads,
                )
                fault_sim_seconds += time.perf_counter() - sim_started
                heartbeat.tick(offset + 1)

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            active_ids = [int(row["id"]) for row in active_rows]

        # M1 (transition twin): grade each accepted launch/capture pair against
        # every still-undetected fault this round, then skip already-covered
        # faults instead of re-solving them. Mirrors the random branch.
        drop_sat = cfg.atpg.fault_drop_sat
        remaining = set(active_ids)
        # Heartbeat for the silent serial transition-solve phase (no parallel wave).
        _solve_hb = SolveHeartbeat(
            log,
            round_idx,
            len(active_ids),
            lambda: _live_detected(effective_db_path, campaign_id),
        )
        _solved_count = 0
        # Per-fault SAT outcome (timeout/unknown) for this round's reason report.
        _sat_outcomes: dict[int, str] = {}
        for fault_id in active_ids:
            if drop_sat and fault_id not in remaining:
                continue
            blocked = sorted(rejected_patterns.get(fault_id, set()))
            tier_timeout = timeout_tiers[
                min(prior_timeout_count.get(fault_id, 0), len(timeout_tiers) - 1)
            ]
            atpg_started = time.perf_counter()
            solved = dict(
                core.solve_transition_fault_atpg(
                    json_path,
                    effective_cell_map,
                    effective_db_path,
                    fault_id,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    tier_timeout,
                    unsupported,
                    bb_instances,
                    cfg.atpg.cone_restrict,
                )
            )
            atpg_seconds += time.perf_counter() - atpg_started
            result = str(solved["result"])
            _solved_count += 1
            _solve_hb.tick(_solved_count)
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
                    bb_instances,
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
                        fault_ids=sorted(remaining) if drop_sat else [fault_id],
                        vector_index=vector_index,
                        unsupported=unsupported,
                        blackbox_instances=bb_instances,
                        sim_threads=sim_threads,
                    )
                    fault_sim_seconds += time.perf_counter() - sim_started
                    if drop_sat:
                        with connect(effective_db_path) as conn:
                            init_schema(conn)
                            remaining.intersection_update(
                                int(r["id"])
                                for r in _active_fault_rows(conn, campaign_id)
                            )
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
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1
                _sat_outcomes[fault_id] = "timeout"
            else:
                stats.unknown += 1
                round_tracker.sat_outcomes.append("UNKNOWN")
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1
                _sat_outcomes[fault_id] = "unknown"

        with connect(effective_db_path) as conn:
            init_schema(conn)
            record_sat_outcomes(conn, campaign_id, _sat_outcomes, round_idx)
            conn.commit()
            data = summary(conn, campaign_id=campaign_id)
            detected, redundant = _fault_counts(conn, campaign_id)
            active_rows_end = _active_fault_rows(conn, campaign_id)
            active_remaining = len(active_rows_end)
            active_ids_end = [int(row["id"]) for row in active_rows_end]
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
        ) and not _escalation_headroom(
            active_ids_end, prior_timeout_count, timeout_tiers
        ):
            terminal = "STALLED"
            break

    log.info("atpg   transition terminated: %s", terminal)

    with connect(effective_db_path) as conn:
        init_schema(conn)
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
