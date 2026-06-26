from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from faultflow.atpg import VectorSet
from faultflow.config import (
    FaultflowConfig,
    interleave_easy_hard,
    parse_timeout_schedule,
)
from faultflow.db import connect, init_schema, summary
from faultflow.db.candidates import (
    CandidateCommit,
    CandidateRejection,
    append_vector_row,
    commit_candidate,
    insert_pending_candidate,
    load_blocked_patterns,
)
from faultflow.runner.parallel_solve import solve_fault_worker
from faultflow.runner.progressive_atpg import (
    ATPGRANDOM_SEED,
    AtpgStats,
    _RoundTracker,
    _escalation_headroom,
    _fault_counts,
    _should_stall,
    pattern_key,
)
from faultflow.runner.runner import RunnerError
from faultflow.scan.atpg_view import PPI_PREFIX, PPO_PREFIX
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.protocol import ScanPattern, serialize_vector
from faultflow.scan.domain_reach import (
    compute_cross_domain_net_ids,
    tag_cross_domain_exclusions,
)
from faultflow.scan.site_resolution import (
    apply_scan_execution_map,
    build_scan_execution_map,
    build_site_key_index,
    fault_type_to_sa_code,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.verify import reduced_protocol_matches
from faultflow.testpoint.preflight import PreflightData, run_preflight

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanPipelineContext:
    cfg: FaultflowConfig
    manifest: dict[str, Any]
    generic_json: Path
    pseudo_port_map: dict[str, dict[str, Any]]
    functional_output_order: list[str]
    q_stem_site_keys: frozenset[str]
    # Fast lookup: Q-stem fault site key → PPO port name in the reduced view.
    # Used to determine Q-stem detection from the capture-frame D-input value,
    # bypassing the full scan protocol simulation for stuck-at faults.
    q_stem_to_ppo: dict[str, str] = field(default_factory=dict)
    # INTEST/EXTEST boundary name-mapping (empty for FUNCTIONAL).
    # Maps fused pseudo-port names back to their generic-netlist counterparts
    # so the golden gate can drive/observe the real (un-fused) netlist.
    wbr_stimulus_name_by_port: dict[str, str] = field(default_factory=dict)
    wbr_observe_name_by_port: dict[str, str] = field(default_factory=dict)
    wbr_decoupled_bits: frozenset[int] = frozenset()


@dataclass
class _FaultRow:
    fault_id: int
    fault_site_key: str
    fault_type: str
    net_index: int = -1
    net_id: int = -1


def build_scan_pipeline_context(
    cfg: FaultflowConfig,
    manifest: dict[str, Any],
    generic_json: Path,
    pseudo_port_map: dict[str, dict[str, Any]],
    functional_output_order: list[str],
    wbr_stimulus_name_by_port: dict[str, str] | None = None,
    wbr_observe_name_by_port: dict[str, str] | None = None,
    wbr_decoupled_bits: frozenset[int] | None = None,
) -> ScanPipelineContext:
    q_stems = {
        str(entry["boundary"]["q_stem_site_key"])
        for entry in pseudo_port_map.values()
        if isinstance(entry.get("boundary"), dict)
    }
    q_stem_to_ppo = {
        str(entry["boundary"]["q_stem_site_key"]): str(entry["ppo_port"])
        for entry in pseudo_port_map.values()
        if isinstance(entry.get("boundary"), dict) and entry.get("ppo_port") is not None
    }
    return ScanPipelineContext(
        cfg=cfg,
        manifest=manifest,
        generic_json=generic_json,
        pseudo_port_map=pseudo_port_map,
        functional_output_order=functional_output_order,
        q_stem_site_keys=frozenset(q_stems),
        q_stem_to_ppo=q_stem_to_ppo,
        wbr_stimulus_name_by_port=wbr_stimulus_name_by_port or {},
        wbr_observe_name_by_port=wbr_observe_name_by_port or {},
        wbr_decoupled_bits=(
            wbr_decoupled_bits if wbr_decoupled_bits is not None else frozenset()
        ),
    )


def _active_fault_rows(conn: sqlite3.Connection, campaign_id: int) -> list[_FaultRow]:
    rows = conn.execute(
        """
        SELECT id, fault_site_key, fault_type, net_id,
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
    return [
        _FaultRow(
            fault_id=int(row["id"]),
            fault_site_key=str(row["fault_site_key"]),
            fault_type=str(row["fault_type"]),
            net_index=int(row["net_index"]),
            net_id=int(row["net_id"]),
        )
        for row in rows
    ]


def _cone_size_map(
    core: Any,
    json_path: str,
    cell_map: str,
    rows: list[_FaultRow],
    unsupported: str,
) -> dict[int, int]:
    """Structural cone size per fault id, for smallest-cone-first ordering.

    One batched C++ call over the shared cached graph (the same graph the solver
    uses, loaded with the default empty blackbox list). The size is purely
    structural, so it is computed once and reused across rounds.
    """
    sizes = core.compute_fault_cone_sizes(
        json_path,
        cell_map,
        [r.fault_id for r in rows],
        [r.net_index for r in rows],
        unsupported,
    )
    return {int(fault_id): int(size) for fault_id, size in sizes.items()}


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


def _mark_preflight_redundant(
    db_path: str,
    campaign_id: int,
    stem_ids: frozenset[int],
    redundancy_model: str,
    q_stem_site_keys: frozenset[str],
    core: Any,
) -> int:
    """Mark SA0/SA1 faults at canceling-path stems UNSAT without any SAT call.

    Reuses the same core.mark_fault_redundant / core.mark_fault_protocol_unresolved
    paths the round loop uses, so DB state stays consistent.
    Returns the count of faults pre-certified.
    """
    if not stem_ids:
        return 0
    placeholders = ",".join("?" for _ in stem_ids)
    with connect(db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            f"""
            SELECT id, fault_site_key
            FROM faults
            WHERE campaign_id = ?
              AND status = 'undetected'
              AND exclusion = 'none'
              AND collapsed_into IS NULL
              AND net_id IN ({placeholders})
            """,
            (campaign_id, *sorted(stem_ids)),
        ).fetchall()
    count = 0
    for row in rows:
        fault_id = int(row["id"])
        site_key = str(row["fault_site_key"])
        if site_key in q_stem_site_keys:
            core.mark_fault_protocol_unresolved(db_path, fault_id)
        else:
            core.mark_fault_redundant(db_path, fault_id, redundancy_model)
        count += 1
    if count:
        log.info("atpg   preflight Phase B: pre-certified %d faults as UNSAT", count)
    return count


def _protocol_fault_sim_kwargs(
    ctx: ScanPipelineContext, pattern: Any
) -> dict[str, object]:
    from faultflow.runner.runner import _port_name_for_net

    # Support both v2 (clock_nets: list) and v1 (clock_net: int) manifests.
    clock_nets_raw = ctx.manifest.get("clock_nets")
    if isinstance(clock_nets_raw, list) and clock_nets_raw:
        clock_net_ids = [int(n) for n in clock_nets_raw]
    else:
        clk = ctx.manifest.get("clock_net")
        if not isinstance(clk, int):
            raise RunnerError(
                "scan manifest must have clock_nets (list) or clock_net (int)"
            )
        clock_net_ids = [clk]
    clock_ports: list[str] = []
    for clk_net in clock_net_ids:
        port = _port_name_for_net(
            ctx.generic_json, str(ctx.manifest["top"]), clk_net, "input"
        )
        if port is None:
            raise RunnerError(f"cannot map scan clock net {clk_net} to a port")
        clock_ports.append(port)
    scan_inputs = ctx.manifest.get("scan_inputs", [])
    scan_outputs = ctx.manifest.get("scan_outputs", [])
    if not isinstance(scan_inputs, list) or not isinstance(scan_outputs, list):
        raise RunnerError("manifest scan_inputs/scan_outputs must be lists")
    # INTEST/EXTEST: remap fused boundary port names to generic netlist names.
    # capture_pi_values carries both __wbi_ stimulus and __wbo_ observe keys, so
    # the rename must cover both maps (see _process_scan_candidate).
    stim_map = ctx.wbr_stimulus_name_by_port
    obs_map = ctx.wbr_observe_name_by_port
    rename = {**stim_map, **obs_map}
    gate_pi_values = (
        {rename.get(k, k): v for k, v in pattern.capture_pi_values.items()}
        if rename
        else pattern.capture_pi_values
    )
    gate_output_ports = (
        [obs_map.get(p, p) for p in ctx.functional_output_order]
        if obs_map
        else ctx.functional_output_order
    )
    return {
        "clock_ports": clock_ports,
        "scan_enable_port": str(ctx.manifest["scan_enable"]),
        "scan_input_ports": [str(name) for name in scan_inputs],
        "scan_output_ports": [str(name) for name in scan_outputs],
        "functional_output_ports": gate_output_ports,
        "max_chain_length": int(ctx.manifest.get("max_chain_length", 0)),
        "load_seqs": pattern.load_seqs,
        "capture_pi_values": gate_pi_values,
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
    ppo_order = [
        str(entry["ppo_port"])
        for _, entry in sorted(pseudo_port_map.items())
        if entry.get("ppo_port") is not None
    ]
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
    """LOC capture vector V2 over reduced PIs.

    Active-domain FFs: capture PPI = launch PPO (next-state).
    Inactive-domain FFs (ppo_port is None): capture PPI = launch PPI (hold).
    Real PIs are held from V1.
    """
    capture = {
        name: bool(value)
        for name, value in launch_vector.items()
        if not str(name).startswith(PPI_PREFIX) and not str(name).startswith(PPO_PREFIX)
    }
    for entry in pseudo_port_map.values():
        ppo = entry.get("ppo_port")
        if ppo is not None:
            capture[str(entry["ppi_port"])] = bool(
                materialized_launch.get(str(ppo), False)
            )
        else:
            # Inactive domain: FF holds its loaded (launch frame) state.
            capture[str(entry["ppi_port"])] = bool(
                launch_vector.get(str(entry["ppi_port"]), False)
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
    active_clock_ports: list[str] | None = None,
) -> tuple[bool, bool, ScanPattern | None]:
    """Return (accepted, protocol_no_progress, accepted_pattern).

    `accepted_pattern` is the canonical block-level ScanPattern when the
    candidate is accepted (for export/retarget), else None.

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
        # serialize: load from V1's PPIs, unload-expectation from active PPOs.
        serialize_input = dict(vector)
        for entry in scan_ctx.pseudo_port_map.values():
            ppo = entry.get("ppo_port")
            if ppo is not None:
                serialize_input[str(ppo)] = bool(
                    reduced_expectation.get(str(ppo), False)
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

    # INTEST/EXTEST: remap fused boundary port names to generic netlist names
    # so the golden gate drives/observes the unwrapped (generic) netlist correctly.
    # `serialize_vector` packs every non-PPI/PPO key (both __wbi_ stimulus AND
    # __wbo_ observe outputs that bled into materialized_launch) into
    # capture_pi_values, so the rename must cover BOTH maps -- otherwise an
    # unmapped __wbo_ key reaches the generic sim as an unknown port.
    _stim_map = scan_ctx.wbr_stimulus_name_by_port
    _obs_map = scan_ctx.wbr_observe_name_by_port
    _rename = {**_stim_map, **_obs_map}
    if _rename:
        gate_scan_pattern = ScanPattern(
            load_seqs=scan_pattern.load_seqs,
            capture_pi_values={
                _rename.get(k, k): v for k, v in scan_pattern.capture_pi_values.items()
            },
            expected_unload=scan_pattern.expected_unload,
        )
    else:
        gate_scan_pattern = scan_pattern
    gate_output_order = (
        [_obs_map.get(p, p) for p in scan_ctx.functional_output_order]
        if _obs_map
        else scan_ctx.functional_output_order
    )
    gate_reduced = (
        {_obs_map.get(k, k): v for k, v in reduced_expectation.items()}
        if _obs_map
        else reduced_expectation
    )
    if not _obs_map:
        # FUNCTIONAL / EXTEST: golden-gate check against the generic netlist.
        # INTEST: skipped — $wbc_{in,out}_scan_faultflow cells are not in the
        # C++ cell map; the generic sim blackboxes them (zeroing all core inputs
        # via TO_CORE and boundary outputs via TO_SYS), so every comparison
        # fails by construction.  Pattern validity is guaranteed by the SAT
        # solver + fault-sim pipeline on the fused ATPG view (no WBR cells).
        try:
            reduced_protocol_matches(
                scan_ctx.cfg,
                scan_ctx.manifest,
                scan_ctx.generic_json,
                gate_scan_pattern,
                reduced_vector=gate_reduced,
                functional_output_order=gate_output_order,
                loc_two_capture=is_loc,
                los_two_capture=is_los,
                los_launch_scan_in=head_bits,
                active_clock_ports=active_clock_ports,
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
            return False, True, None

    # Build pre-loaded records from active_rows (already in memory — no DB round-trips).
    # Each tuple: (fault_id, compiled_net_index, type: 0=SA0/1=SA1).
    # active_rows may contain stale (already-detected) faults between random vectors;
    # mark_fault_detected guards against overwrites with AND status='undetected'.
    _preloaded = [
        (row.fault_id, row.net_index, 1 if row.fault_type == "SA1" else 0)
        for row in active_rows
    ]
    if transition:
        tentative = list(
            core.simulate_transition_tentative_preloaded(
                reduced_json_path,
                reduced_cell_map,
                _preloaded,
                vector,
                capture_vector,
                input_order,
                unsupported,
            )
        )
    else:
        tentative = list(
            core.simulate_tentative_preloaded(
                reduced_json_path,
                reduced_cell_map,
                _preloaded,
                vector,
                input_order,
                unsupported,
            )
        )
    # PERF (CVA6-scale lever): the reduced pseudo-PI/PO view grades any logic-cone
    # fault exactly as the full load+capture+unload scan protocol would. The shift
    # is a fault-INDEPENDENT permutation, and the per-candidate golden gate above
    # (reduced_protocol_matches) proves fault-free reduced==protocol on every
    # unload bit + functional PO. So a logic-cone fault observed at an active PPO
    # in the reduced view is necessarily captured and shifted out by the real
    # protocol -- no need to re-simulate the thousand-cycle protocol for it. Only
    # FF Q-stem faults (whose net is abstracted into a pseudo-port and is thus
    # invisible to the reduced view) require the full protocol sim to confirm.
    qstem_active = set(_active_q_stem_fault_ids(active_rows, scan_ctx.q_stem_site_keys))
    reduced_trusted = sorted(set(tentative) - qstem_active)
    protocol_sim_fault_ids = sorted(qstem_active)

    if not reduced_trusted and not protocol_sim_fault_ids:
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
        return False, source == "sat", None

    # Logic-cone faults are trusted directly from the cheap reduced grade.
    passed_fault_ids: list[int] = list(reduced_trusted)
    protocol_sim_rejections: list[CandidateRejection] = []
    blocked: list[tuple[int, str]] = []

    # Q-stem faults: their Q-net is a PPI in the reduced view, so the fault-free
    # captured D-input value (PPO) directly determines detection.
    # For stuck-at SA0: detected iff D_captured=1 (natural=1, stuck=0 → mismatch).
    # For stuck-at SA1: detected iff D_captured=0 (natural=0, stuck=1 → mismatch).
    # Every scan FF is in the shift-out chain, so activation equals observation.
    # For transition faults the detection depends on the launch/capture transition
    # rather than just the capture value, so those still use the full protocol sim.
    if protocol_sim_fault_ids:
        if not transition:
            row_by_id = {row.fault_id: row for row in active_rows}
            for fault_id in protocol_sim_fault_ids:
                row = row_by_id.get(fault_id)
                if row is None:
                    continue
                ppo_port = scan_ctx.q_stem_to_ppo.get(row.fault_site_key)
                if ppo_port is None:
                    continue
                cap_val = bool(reduced_expectation.get(ppo_port, False))
                if row.fault_type == "SA0":
                    if cap_val:
                        passed_fault_ids.append(fault_id)
                    elif source == "sat" and fault_id == sat_target_fault_id:
                        protocol_sim_rejections.append(
                            CandidateRejection(fault_id, "q_stem_no_capture_effect")
                        )
                        blocked.append((fault_id, track_key))
                else:
                    if not cap_val:
                        passed_fault_ids.append(fault_id)
                    elif source == "sat" and fault_id == sat_target_fault_id:
                        protocol_sim_rejections.append(
                            CandidateRejection(fault_id, "q_stem_no_capture_effect")
                        )
                        blocked.append((fault_id, track_key))
        else:
            # Transition faults: full protocol sim required (two-frame detection).
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

            protocol_fault_sim_kwargs = _protocol_fault_sim_kwargs(
                scan_ctx, scan_pattern
            )
            protocol_fault_sim_result = dict(
                core.simulate_scan_protocol_faults(
                    str(scan_ctx.generic_json),
                    generic_cell_map,
                    faults=protocol_fault_specs,
                    unsupported_policy=unsupported,
                    loc_two_capture=is_loc,
                    los_two_capture=is_los,
                    los_launch_scan_in=head_bits,
                    active_clock_ports=active_clock_ports or [],
                    **protocol_fault_sim_kwargs,
                )
            )
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
        return False, source == "sat", None

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
    return True, False, scan_pattern


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
    scan_pattern_out: Path | None = None,
) -> tuple[VectorSet, AtpgStats, int, float, float]:
    from faultflow.runner.runner import _load_core, _port_names

    # Accepted block-level scan patterns, collected for export when
    # scan_pattern_out is set (used by SoC retargeting). One per accepted vector.
    accepted_patterns: list[ScanPattern] = []

    # S4.8 — the 1-FF WBC is correct ONLY under single-capture INTEST.
    # LOC/LOS two-capture sequences clobber q before the second frame, so a
    # scan-WBR block with wbr_model="scan" must never run transition ATPG until
    # the 2-FF upgrade lands. Fail loudly rather than silently mis-deliver.
    if cfg.wbr_model == "scan" and transition:
        raise ScanError(
            "wbr_model='scan' does not support transition (LOC/LOS) ATPG: "
            "the 1-FF WBC delivers stimulus for exactly one capture cycle; "
            "a second capture clobbers q and corrupts boundary stimulus. "
            "Use wbr_model='buffer' for transition faults, or upgrade to a "
            "2-FF WBC first."
        )

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
        scan_ctx.wbr_decoupled_bits,
    )
    # For transition faults in multi-domain designs, tag cross-domain fault sites.
    if cfg.fault_model.model == "transition":
        clock_nets_raw = scan_ctx.manifest.get("clock_nets", [])
        if isinstance(clock_nets_raw, list) and len(clock_nets_raw) > 1:
            cross_domain_ids = compute_cross_domain_net_ids(
                scan_ctx.generic_json, scan_ctx.manifest
            )
            if cross_domain_ids:
                generic_rows: list[dict[str, Any]] = list(
                    core.list_site_keys(
                        str(scan_ctx.generic_json), generic_cell_map, unsupported
                    )
                )
                exclusions = tag_cross_domain_exclusions(
                    generic_rows, cross_domain_ids, exclusions
                )
                n_xd = sum(1 for v in exclusions.values() if v == "cross_domain")
                log.info(
                    "cross-domain: tagged %d fault sites excluded_cross_domain",
                    n_xd,
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

    # Escalating SAT timeout: each fault starts at the smallest tier and only
    # moves up a tier once it has timed out. prior_timeout_count persists across
    # rounds so a fault that keeps timing out is given progressively more budget.
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

    # Cone-size fault ordering (atpg.order_by_cone_size): the structural cone size
    # is computed once (it never changes) and reused to sort each round's SAT
    # sweep smallest-cone-first. Skipped for transition faults, whose two-frame
    # cone is not captured by the single-frame size (they keep enumeration order).
    cone_sizes: dict[int, int] = {}
    order_faults = cfg.atpg.order_by_cone_size and not transition
    if cfg.atpg.order_by_cone_size and transition:
        log.info(
            "atpg   cone-size ordering not applied to transition faults; "
            "using enumeration order"
        )

    # Parallel SAT solving: warm the process-local graph cache before forking so
    # child processes inherit the compiled graph via copy-on-write (read-only pages
    # are shared without re-loading). Workers==1 means serial; fork unavailable on
    # Windows/spawn but we run in WSL where fork is always present.
    _parallel = cfg.atpg.workers > 1
    _executor: ProcessPoolExecutor | None = None
    if _parallel:
        log.info(
            "atpg   warming graph cache before forking %d worker processes",
            cfg.atpg.workers,
        )
        core.compute_fault_cone_sizes(
            reduced_json_path, reduced_cell_map, [], [], unsupported
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

    # Determine the solve kind once — same for every fault in this campaign.
    if los:
        _solve_kind = "los_transition"
    elif transition:
        _solve_kind = "broadside_transition"
    else:
        _solve_kind = "scan_stuck_at"

    # OT structural reconvergence preflight (cfg.atpg.preflight, stuck-at only).
    # Phase A: reconvergent-site faults are sorted last and their SAT timeout tier
    # is bumped by 1 so the short (2 s) tier is skipped — these faults are likely
    # hard or near-redundant and waste the short budget.
    # Phase B: faults at canceling-path stems are marked UNSAT without any SAT call.
    # Falls back silently if opentest is not on PATH or the preflight subprocess fails.
    _preflight: PreflightData | None = None
    _reconv_ids: frozenset[int] = frozenset()
    if cfg.atpg.preflight and not transition:
        _opentest_bin = shutil.which("opentest") or shutil.which("opentest-cli")
        if _opentest_bin:
            _lib = str(cfg.cell_lib).lower()
            _tech = cfg.atpg.preflight_tech or (
                "osu035" if ("osu035" in _lib or "osu" in _lib) else "sky130"
            )
            _preflight = run_preflight(
                Path(reduced_json_path),
                Path(reduced_json_path).parent / "preflight",
                _opentest_bin,
                _tech,
            )
            if _preflight:
                _reconv_ids = _preflight.fanout_yosys_ids
                log.info(
                    "atpg   preflight: %d reconvergent stems, %d canceling stems",
                    len(_preflight.fanout_yosys_ids),
                    len(_preflight.redundant_stem_ids),
                )
                if _preflight.redundant_stem_ids:
                    _mark_preflight_redundant(
                        effective_db_path,
                        campaign_id,
                        _preflight.redundant_stem_ids,
                        redundancy_model,
                        scan_ctx.q_stem_site_keys,
                        core,
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
                accepted, protocol_no_progress, accepted_pattern = (
                    _process_scan_candidate(
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
                )
            fault_sim_seconds += time.perf_counter() - sim_started
            if accepted:
                vectors.append(vector)
                stats.accepted_vectors += 1
                if accepted_pattern is not None:
                    accepted_patterns.append(accepted_pattern)
            else:
                stats.rejected_candidates += 1
                if protocol_no_progress:
                    round_tracker.sat_outcomes.append("protocol_no_progress")
                    stats.protocol_no_progress_rounds += 1

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            db_blocked = load_blocked_patterns(conn, campaign_id)

        # Phase A: seed a tier-skip for reconvergent-site faults so they start at
        # the second timeout tier (skipping the short 2 s attempt). This is done
        # once per round, before the sort and before parallel dispatch, so both
        # paths pick up the updated prior_timeout_count.
        if _reconv_ids:
            for _row in active_rows:
                if _row.net_id in _reconv_ids:
                    prior_timeout_count[_row.fault_id] = max(
                        1, prior_timeout_count.get(_row.fault_id, 0)
                    )

        # Sort: (1) reconvergent-site faults last, (2) smallest cone first within
        # each group. Both criteria are independent; either can be inactive.
        if (order_faults or _reconv_ids) and active_rows:
            if order_faults and not cone_sizes:
                cone_sizes = _cone_size_map(
                    core,
                    reduced_json_path,
                    reduced_cell_map,
                    active_rows,
                    unsupported,
                )
            active_rows = sorted(
                active_rows,
                key=lambda r: (
                    r.net_id in _reconv_ids,
                    cone_sizes.get(r.fault_id, 1 << 30) if order_faults else 0,
                ),
            )

        current_active_ids = {row.fault_id for row in active_rows}

        # Parallel: pre-solve all active faults in the worker pool before the
        # processing loop.  workers==1 leaves _parallel_results empty and the
        # serial solve path below runs unchanged (A/B control).
        _parallel_results: dict[int, tuple[str, dict]] = {}
        if _parallel and _executor is not None and active_rows:
            # Build the submission order: when easy_fault_reserve > 0 and
            # workers >= 4, interleave easy faults (front of sorted list)
            # with hard faults (back) so each parallel chunk has both. The
            # processing loop reads results from the dict by fault_id so it
            # is unaffected by submission order.
            _dispatch_rows = (
                interleave_easy_hard(active_rows, cfg.atpg.workers, _easy_reserve)
                if _easy_reserve > 0 and cfg.atpg.workers >= 4
                else active_rows
            )
            _wave_args: list[Any] = [
                (
                    _solve_kind,
                    reduced_json_path,
                    reduced_cell_map,
                    effective_db_path,
                    row.fault_id,
                    sorted(
                        db_blocked.get(row.fault_id, set())
                        | rejected_patterns.get(row.fault_id, set())
                    ),
                    cfg.atpg.sat_conflict_limit,
                    timeout_tiers[
                        min(
                            prior_timeout_count.get(row.fault_id, 0),
                            len(timeout_tiers) - 1,
                        )
                    ],
                    unsupported,
                    cfg.atpg.cone_restrict,
                    list(los_couple_ports) if los else [],
                    list(los_head_ports) if los else [],
                    [],  # bb_instances: not needed in scan fused-view path
                    "",  # test_mode: baked into the fused-view netlist
                )
                for row in _dispatch_rows
            ]
            _wave_started = time.perf_counter()
            try:
                for _fid, _res, _slv in _executor.map(solve_fault_worker, _wave_args):
                    _parallel_results[int(_fid)] = (_res, dict(_slv))
            except Exception as _exc:
                log.warning(
                    "atpg   parallel wave error (%s); "
                    "faults absent from results will be treated as UNKNOWN",
                    _exc,
                )
            atpg_seconds += time.perf_counter() - _wave_started

        for row in active_rows:
            fault_id = row.fault_id
            if fault_id not in current_active_ids:
                continue
            blocked = sorted(
                db_blocked.get(fault_id, set()) | rejected_patterns.get(fault_id, set())
            )
            tier_timeout = timeout_tiers[
                min(prior_timeout_count.get(fault_id, 0), len(timeout_tiers) - 1)
            ]
            if _parallel_results:
                # Parallel path: result already computed by a worker process.
                _pre_res, _pre_slv = _parallel_results.get(
                    fault_id, ("UNKNOWN", {"result": "UNKNOWN"})
                )
                solved = dict(_pre_slv)
                result = _pre_res
            else:
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
                            tier_timeout,
                            unsupported,
                            cone_restrict=cfg.atpg.cone_restrict,
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
                            tier_timeout,
                            unsupported,
                            cone_restrict=cfg.atpg.cone_restrict,
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
                            tier_timeout,
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
                    accepted, protocol_no_progress, accepted_pattern = (
                        _process_scan_candidate(
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
                    )
                fault_sim_seconds += time.perf_counter() - sim_started
                if accepted:
                    seen_patterns.add(key)
                    vectors.append(candidate)
                    stats.accepted_vectors += 1
                    round_tracker.sat_outcomes.append("SAT")
                    if accepted_pattern is not None:
                        accepted_patterns.append(accepted_pattern)
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
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1
            else:
                stats.unknown += 1
                round_tracker.sat_outcomes.append("UNKNOWN")
                prior_timeout_count[fault_id] = prior_timeout_count.get(fault_id, 0) + 1

        with connect(effective_db_path) as conn:
            init_schema(conn)
            data = summary(conn, campaign_id=campaign_id)
            detected, redundant = _fault_counts(conn, campaign_id)
            active_rows_end = _active_fault_rows(conn, campaign_id)
            active_remaining = len(active_rows_end)
            active_ids_end = [row.fault_id for row in active_rows_end]
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
            with connect(effective_db_path) as conn:
                init_schema(conn)
                _termination_sweep_q_stems(conn, campaign_id, scan_ctx.q_stem_site_keys)
            terminal = "STALLED"
            break

    if _executor is not None:
        _executor.shutdown(wait=False)

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
    if scan_pattern_out is not None and accepted_patterns:
        from faultflow.scan.pattern_export import scan_pattern_to_dict

        scan_pattern_out.parent.mkdir(parents=True, exist_ok=True)
        scan_pattern_out.write_text(
            json.dumps([scan_pattern_to_dict(p) for p in accepted_patterns], indent=2),
            encoding="utf-8",
        )
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
