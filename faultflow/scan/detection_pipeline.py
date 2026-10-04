from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
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
from faultflow.db import connect, init_schema, record_sat_outcomes, summary
from faultflow.db.candidates import (
    CandidateCommit,
    CandidateRejection,
    append_vector_row,
    commit_candidate,
    insert_pending_candidate,
    load_blocked_patterns,
    load_rejection_reasons,
)
from faultflow.runner.parallel_solve import install_seed, solve_fault_worker
from faultflow.runner.progressive_atpg import (
    ATPGRANDOM_SEED,
    AtpgStats,
    GradeHeartbeat,
    SolveHeartbeat,
    _RoundTracker,
    _coverage_denominator,
    _escalation_headroom,
    _fault_counts,
    _live_detected,
    _random_stop_reached,
    _should_stall,
    pattern_key,
)
from faultflow.runner.runner import RunnerError
from faultflow.scan.atpg_view import (
    PPI_PREFIX,
    PPO_PREFIX,
    UNOBSERVED_NETS_ATTR,
    make_blackbox_transparent,
    make_nonscan_free,
)
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.stitch import _top_module
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
from faultflow.scan.compaction import CompactionMap, build_compactor_fanout
from faultflow.scan.compression import CompressionMap, build_broadcast_fanout
from faultflow.scan.errors import ScanError
from faultflow.scan.nonscan import NonscanSetup, jtag_sites, settled_sites
from faultflow.scan.shift_controls import reset_holds
from faultflow.scan.ring_generator import (
    bitmask_to_index_list,
    care_bit_rows,
    lookup_polynomial,
)
from faultflow.scan.verify import reduced_protocol_matches
from faultflow.scan.x_mask import (
    XMask,
    apply_x_mask,
    compute_x_mask,
    launch_mode_key,
    recorded_x_mask,
    x_source_nets,
)
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
    # Async set/reset primary inputs held at their INACTIVE level for the whole
    # scan test (standard scan DFT). Maps PI name -> inactive value. Keeps the
    # reduced view (which ignores the FF's async control) consistent with the full
    # scan protocol (which applies it at capture). Empty unless the design has
    # scannable async-reset/set FFs whose control pin is a primary input.
    reset_pi_holds: dict[str, bool] = field(default_factory=dict)
    # Scan compression (faultflow.scan.compression): None unless
    # [compression] enabled = true. compression_map.phase_shifter_taps and
    # compression_care_bit_rows are both reconstructed deterministically from
    # cfg.compression.channels + the manifest's chain count -- pure functions
    # of the same inputs insert_compression used, so no persisted manifest
    # section is needed to rebuild them (see build_scan_pipeline_context).
    # care_bit_rows is precomputed ONCE per campaign, not per-candidate, over
    # max_chain_length + 1 shift cycles: the load, then the launch-on-shift
    # shift.
    compression_map: CompressionMap | None = None
    compression_care_bit_rows: list[list[int]] | None = None
    # The decompressor loads every scan cell from one seed per pattern: per
    # PPI, the seed bits it XORs into that cell (chain c's position p loads at
    # shift cycle max_chain_length-1-p); per chain head PPI, those of the bit
    # a launch-on-shift shift brings in. Every scan solve is constrained by
    # them (_seed_kwargs), so SAT only finds loads the decompressor can make.
    seeded_inputs: dict[str, list[int]] = field(default_factory=dict)
    seeded_heads: dict[str, list[int]] = field(default_factory=dict)
    # Scan compaction (faultflow.scan.compaction): None unless
    # [compaction] enabled = true. Reconstructed deterministically from
    # cfg.compaction.channels + the manifest's chain count -- the same
    # pure-function-of-inputs shape insert_compaction used, so no persisted
    # manifest section is needed to rebuild it. Simpler than compression_map:
    # no algebra to precompute ahead of time, just the fanout map itself.
    # Relies on manifest["scan_outputs"]'s order matching the order
    # insert_compaction used to build the real compactor's fanout -- true
    # today by construction (a static artifact written once).
    compaction_map: CompactionMap | None = None
    # Precomputed ONCE per campaign (build_scan_pipeline_context), not
    # per-candidate: resolving a clock net id to its port name
    # (_port_name_for_net) does a full, uncached JSON parse of generic_json,
    # and this is otherwise static for the whole campaign. Previously
    # recomputed inside _protocol_fault_sim_kwargs, called once per
    # candidate -- a real cost at scale (the "Python per-candidate JSON
    # re-parse in _protocol_fault_sim_kwargs" lever from cva6_perf_roadmap.md).
    clock_ports: list[str] = field(default_factory=list)
    # The observation points a blackbox output's unknown value reaches, for
    # this campaign's launch mode (faultflow.scan.x_mask): no detection credit,
    # no SAT target, don't-care in every pattern. Empty without blackboxes.
    x_mask: XMask = XMask()
    # [scan] nonscan_cells / [scan] hold (faultflow.scan.nonscan): the view ties
    # the non-scan flops and the held inputs, so the held inputs aren't view
    # ports; every pattern applies them in every cycle (_serialize). None and
    # empty without non-scan cells.
    nonscan: NonscanSetup | None = None
    input_holds: dict[str, bool] = field(default_factory=dict)


@dataclass
class _FaultRow:
    fault_id: int
    fault_site_key: str
    fault_type: str
    net_index: int = -1
    net_id: int = -1


def _scan_reset_pi_holds(
    generic_json: Path, top: str, cell_map: dict[str, Any]
) -> dict[str, bool]:
    """Primary inputs that reach a scannable async-reset/set FF's control pin
    through buffers and inverters, mapped to the value that keeps the pin INACTIVE,
    held during the whole scan test (scan.shift_controls.reset_holds).

    An input two pins need at opposite values isn't held, and a control driven by
    other logic can't be held through an input: scan-check refuses a scan flop whose
    control isn't held inactive during shift.
    """
    try:
        data = json.loads(Path(generic_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    module = data.get("modules", {}).get(top)
    if not isinstance(module, dict):
        return {}
    return {port: bool(value) for port, value in reset_holds(module, cell_map).items()}


def build_scan_pipeline_context(
    cfg: FaultflowConfig,
    manifest: dict[str, Any],
    generic_json: Path,
    pseudo_port_map: dict[str, dict[str, Any]],
    functional_output_order: list[str],
    wbr_stimulus_name_by_port: dict[str, str] | None = None,
    wbr_observe_name_by_port: dict[str, str] | None = None,
    wbr_decoupled_bits: frozenset[int] | None = None,
    x_mask: XMask | None = None,
    nonscan: NonscanSetup | None = None,
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
    try:
        cell_map = json.loads(cfg.cell_lib.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cell_map = {}
    reset_pi_holds = _scan_reset_pi_holds(
        generic_json, str(manifest.get("top", cfg.top)), cell_map
    )
    from faultflow.runner.runner import _port_name_for_net

    clock_ports: list[str] = []
    for clk_net in manifest_clock_net_ids(manifest, error_cls=RunnerError):
        port = _port_name_for_net(
            generic_json, str(manifest.get("top", cfg.top)), clk_net, "input"
        )
        if port is None:
            raise RunnerError(f"cannot map scan clock net {clk_net} to a port")
        clock_ports.append(port)
    compression_map: CompressionMap | None = None
    compression_care_bit_rows: list[list[int]] | None = None
    seeded_inputs: dict[str, list[int]] = {}
    seeded_heads: dict[str, list[int]] = {}
    if cfg.compression.enabled:
        num_chains = len(manifest.get("scan_inputs", []))
        polynomial = lookup_polynomial(cfg.compression.channels)
        phase_shifter_taps = build_broadcast_fanout(
            cfg.compression.channels, num_chains
        )
        compression_map = CompressionMap(
            cfg.compression.channels, polynomial, phase_shifter_taps
        )
        max_chain_length = int(manifest.get("max_chain_length", 0))
        compression_care_bit_rows = care_bit_rows(
            polynomial, phase_shifter_taps, max_chain_length + 1
        )
        for entry in pseudo_port_map.values():
            chain = int(entry["chain_id"])
            position = int(entry["position_in_chain"])
            if not 0 <= chain < num_chains or not 0 <= position < max_chain_length:
                raise ScanError(
                    f"scan cell {entry['ppi_port']} (chain {chain}, position "
                    f"{position}) is outside the {num_chains} compressed chains "
                    f"of at most {max_chain_length} cells"
                )
            ppi = str(entry["ppi_port"])
            seeded_inputs[ppi] = bitmask_to_index_list(
                compression_care_bit_rows[max_chain_length - 1 - position][chain]
            )
            if position == 0:
                seeded_heads[ppi] = bitmask_to_index_list(
                    compression_care_bit_rows[max_chain_length][chain]
                )
    compaction_map: CompactionMap | None = None
    if cfg.compaction.enabled:
        num_scan_out_chains = len(manifest.get("scan_outputs", []))
        compaction_map = CompactionMap(
            cfg.compaction.channels,
            build_compactor_fanout(cfg.compaction.channels, num_scan_out_chains),
        )
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
        reset_pi_holds=reset_pi_holds,
        compression_map=compression_map,
        compression_care_bit_rows=compression_care_bit_rows,
        seeded_inputs=seeded_inputs,
        seeded_heads=seeded_heads,
        compaction_map=compaction_map,
        clock_ports=clock_ports,
        x_mask=x_mask if x_mask is not None else XMask(),
        nonscan=nonscan,
        input_holds=(
            {port: bool(value) for port, value in nonscan.holds.items()}
            if nonscan is not None
            else {}
        ),
    )


def _require_x_mask(
    scan_ctx: ScanPipelineContext, reduced_json_path: str, mode: str | None
) -> None:
    """The view SAT and every reduced simulator run on must mask exactly what
    scan_ctx.x_mask says, computed for this campaign's launch mode. A view
    with an unknown blackbox output and no mask would silently credit
    detections that depend on the memory's content -- or on a non-scan flop's
    unknown state."""
    x_instances = scan_ctx.cfg.blackbox_x_instances
    if not x_instances and not (
        scan_ctx.nonscan is not None and scan_ctx.nonscan.x_sources
    ):
        return
    top = str(scan_ctx.manifest["top"])
    view = json.loads(Path(reduced_json_path).read_text(encoding="utf-8"))
    _, module = _top_module(view, top)
    if not x_source_nets(module, x_instances):
        return
    recorded = recorded_x_mask(view, top)
    if recorded is None:
        raise ScanError(
            "the scan ATPG view has blackbox outputs of unknown value but no X "
            "mask (faultflow.scan.x_mask.compute_x_mask)"
        )
    if recorded != scan_ctx.x_mask.nets or scan_ctx.x_mask.launch_mode != mode:
        raise ScanError(
            "the scan ATPG view's X mask does not match the scan context's, or "
            f"was computed for launch mode {scan_ctx.x_mask.launch_mode!r}, not "
            f"{mode!r}"
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
          AND compression_unresolved = 0
          AND compaction_unresolved = 0
          AND blackbox_unresolved = 0
          AND hold_unresolved = 0
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


def _protocol_fault_sim_kwargs(
    ctx: ScanPipelineContext, pattern: Any
) -> dict[str, object]:
    # ctx.clock_ports is precomputed ONCE per campaign
    # (build_scan_pipeline_context) -- resolving a clock net id to its port
    # name does a full, uncached JSON parse of generic_json, and this
    # function is called once per candidate, so recomputing it here every
    # time was a real per-candidate cost at scale.
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
    # No credit where a blackbox output's unknown value lands: its functional
    # outputs aren't compared, nor its unload bits (pattern.unload_mask).
    observed_outputs = _observed_outputs(ctx)
    gate_output_ports = (
        [obs_map.get(p, p) for p in observed_outputs] if obs_map else observed_outputs
    )
    return {
        "clock_ports": ctx.clock_ports,
        "scan_enable_port": str(ctx.manifest["scan_enable"]),
        "scan_input_ports": [str(name) for name in scan_inputs],
        "scan_output_ports": [str(name) for name in scan_outputs],
        "functional_output_ports": gate_output_ports,
        "max_chain_length": int(ctx.manifest.get("max_chain_length", 0)),
        "load_seqs": pattern.load_seqs,
        "capture_pi_values": gate_pi_values,
        "blackbox_instances": list(ctx.cfg.blackbox_instances),
        "unload_mask": pattern.unload_mask or {},
        "preamble_cycles": pattern.preamble_cycles,
        "shift_pi_values": {
            rename.get(k, k): v for k, v in pattern.shift_pi_values.items()
        },
    }


def _launch_known(ctx: ScanPipelineContext, row: _FaultRow) -> bool:
    """False for a Q-stem of a flop that captures a blackbox output's unknown
    value at the launch-on-capture launch edge: its state after launch is
    unknown, so whether its Q makes a transition is too, and no test may be
    credited with one."""
    return ctx.q_stem_to_ppo.get(row.fault_site_key) not in ctx.x_mask.launch_ppo_ports


def _observed_outputs(ctx: ScanPipelineContext) -> list[str]:
    """The functional outputs a test may compare: all but the ones a blackbox
    output's unknown value reaches."""
    return [p for p in ctx.functional_output_order if p not in ctx.x_mask.outputs]


def _serialize(
    ctx: ScanPipelineContext, serialize_input: dict[str, bool]
) -> ScanPattern:
    """serialize_vector, with the X-masked expected bits marked don't-care, the
    held inputs, which the view ties rather than lists, at their values, the
    preamble that settles the non-scan flops the scan clock settles, and, testing
    the reset, the reset holds while the chains shift."""
    pattern = serialize_vector(
        {**serialize_input, **ctx.input_holds},
        ctx.pseudo_port_map,
        ctx.manifest,
        masked_ppo_ports=ctx.x_mask.ppo_ports,
        masked_outputs=ctx.x_mask.outputs,
    )
    preamble = ctx.nonscan.preamble if ctx.nonscan is not None else 0
    if ctx.compression_map is not None:
        # The decompressor reseeds on scan enable's rising edge, seen through a
        # flop of the previous scan enable (compression.py): a pulse with it off
        # before the load arms it. Back to back, a pattern's load follows the
        # last one's unload, scan enable on, and a chip powers up with that flop
        # unknown -- without the pulse the load continues the old sequence.
        preamble = max(preamble, 1)
    # Testing the reset, a capture may set a reset input active (_process_scan_
    # candidate leaves it free); it stays inactive while the chains shift, where
    # a real scan flop's reset would wipe the load (scan.shift_controls).
    shift = dict(ctx.reset_pi_holds) if ctx.cfg.fault_model.include_reset_faults else {}
    return replace(pattern, preamble_cycles=preamble, shift_pi_values=shift)


def _seed_kwargs(
    scan_ctx: ScanPipelineContext, *, los: bool = False
) -> dict[str, object]:
    """A scan solve's decompressor constraints (the solvers' seeded_inputs, and
    launching on shift seeded_heads): none without compression."""
    if scan_ctx.compression_map is None:
        return {}
    kwargs: dict[str, object] = {
        "seed_width": scan_ctx.compression_map.num_channels,
        "seeded_inputs": scan_ctx.seeded_inputs,
    }
    if los:
        kwargs["seeded_heads"] = scan_ctx.seeded_heads
    return kwargs


def _seeded_bit(row: int, seed: int) -> bool:
    """The scan-in bit the decompressor gives for `seed`, where `row` is the
    bit's care_bit_rows bitmask over the seed bits."""
    return bin(row & seed).count("1") % 2 == 1


def _decompressed(
    core: Any,
    scan_ctx: ScanPipelineContext,
    vector: dict[str, bool],
    pattern: ScanPattern,
    head_bits: dict[int, bool],
) -> ScanPattern:
    """`pattern` as the decompressor shifts it in. The seed is the one that
    loads each scan cell with its value in `vector` -- and, launching on
    shift, brings in `head_bits` (by chain) at the launch shift. Every chain's
    scan-in at every load cycle is that seed's, the cycles whose bits pass
    through a shorter chain included, and every position is care (load_care):
    a tester applies only the seed, so this is exactly what the chip loads.
    Raises ScanError when no seed gives these values: every compressed
    candidate comes from a seed (the seeded SAT solve, _seeded_random_vectors),
    so that would be a bug."""
    assert scan_ctx.compression_map is not None
    rows = scan_ctx.compression_care_bit_rows
    assert rows is not None
    max_chain_length = len(rows) - 1
    names = sorted(scan_ctx.seeded_inputs)
    fanout = [scan_ctx.seeded_inputs[name] for name in names]
    values = [bool(vector[name]) for name in names]
    for chain, bit in sorted(head_bits.items()):
        fanout.append(bitmask_to_index_list(rows[max_chain_length][chain]))
        values.append(bool(bit))
    solved = core.solve_xor_broadcast(
        scan_ctx.compression_map.num_channels, fanout, list(enumerate(values))
    )
    if not solved["ok"]:
        raise ScanError(
            "a compressed scan pattern loads values no decompressor seed gives"
        )
    seed = sum(1 << k for k, bit in enumerate(solved["channels"]) if bit)
    load_seqs = {
        chain: [
            _seeded_bit(rows[cycle][chain], seed) for cycle in range(max_chain_length)
        ]
        for chain in pattern.load_seqs
    }
    for entry in scan_ctx.pseudo_port_map.values():
        chain = int(entry["chain_id"])
        cycle = max_chain_length - 1 - int(entry["position_in_chain"])
        if load_seqs[chain][cycle] != pattern.load_seqs[chain][cycle]:
            raise ScanError(
                f"scan cell {entry['ppi_port']} is not loaded at the shift cycle "
                "its decompressor row is for"
            )
    return replace(
        pattern,
        load_seqs=load_seqs,
        load_care=tuple(
            (chain, cycle)
            for chain in sorted(load_seqs)
            for cycle in range(max_chain_length)
        ),
    )


def _seeded_random_vectors(
    core: Any,
    input_order: list[str],
    scan_ctx: ScanPipelineContext,
    count: int,
    *,
    los: bool = False,
) -> list[tuple[dict[str, bool], dict[int, bool]]]:
    """Random patterns for a compressed design: random real inputs and a random
    seed, each scan cell -- and, launching on shift, each chain's launch
    scan-in bit (by chain) -- what the decompressor makes of the seed. Random
    loads would almost never be a load it can make: one seed of [compression]
    channels bits loads every cell."""
    assert scan_ctx.compression_map is not None
    rows = scan_ctx.compression_care_bit_rows
    assert rows is not None
    seed_names = [f"__seed[{k}]" for k in range(scan_ctx.compression_map.num_channels)]
    free = [name for name in input_order if name not in scan_ctx.seeded_inputs]
    drawn: list[tuple[dict[str, bool], dict[int, bool]]] = []
    for raw in core.atpg_random_vectors(free + seed_names, count, ATPGRANDOM_SEED):
        seed = sum(1 << k for k, name in enumerate(seed_names) if raw[name])
        values = {name: bool(raw[name]) for name in free}
        for ppi, bits in scan_ctx.seeded_inputs.items():
            values[ppi] = sum((seed >> bit) & 1 for bit in bits) % 2 == 1
        heads = (
            {chain: _seeded_bit(row, seed) for chain, row in enumerate(rows[-1])}
            if los
            else {}
        )
        drawn.append(({name: values[name] for name in input_order}, heads))
    return drawn


def _reject_compaction_indistinguishable(
    fault_ids: list[int],
    track_key: str,
    protocol_sim_rejections: list[CandidateRejection],
    blocked: list[tuple[int, str]],
) -> None:
    for fault_id in fault_ids:
        protocol_sim_rejections.append(
            CandidateRejection(fault_id, "compaction_indistinguishable")
        )
        blocked.append((fault_id, track_key))


def _is_compaction_only_rejected(reasons: set[str] | None) -> bool:
    """True iff this fault has been rejected at least once, and EVERY
    rejection reason recorded against it (this campaign, all rounds so far)
    is "compaction_indistinguishable" -- i.e. every witness detected it
    through the real, uncompacted scan-out ports, but its diff aliased to
    zero at every compacted output bit, every cycle. A MIXED history (some
    compaction, some other reason) deliberately falls through to the
    existing mark_fault_redundant behavior unchanged: a mix cannot prove the
    UNSAT is solely attributable to compaction.
    """
    return bool(reasons) and reasons == {"compaction_indistinguishable"}


@dataclass(frozen=True)
class _Twin:
    """A twin of the scan ATPG view that frees what the view ties -- the hold
    twin (atpg_view.make_nonscan_free) or the blackbox-transparent one
    (make_blackbox_transparent) -- and each fault site's compiled net index in
    it."""

    json_path: str
    net_index_by_site: dict[str, int]


def _mapped_twin(
    core: Any,
    scan_ctx: ScanPipelineContext,
    view: dict[str, Any],
    twin_path: Path,
    generic_cell_map: str,
    reduced_cell_map: str,
    unsupported: str,
    blackbox_instances: list[str],
    execution_map: dict[str, int],
    what: str,
) -> _Twin:
    """Write the twin `view` beside the view and map it by the same
    build_scan_execution_map. Only ties, ports and the mask differ between the
    two, so both must map exactly the same fault sites -- anything else is a
    bug in the twin, raised here rather than mid-round."""
    twin_path.write_text(json.dumps(view) + "\n", encoding="utf-8")
    twin_map, _ = build_scan_execution_map(
        core,
        scan_ctx.generic_json,
        twin_path,
        generic_cell_map,
        reduced_cell_map,
        unsupported,
        scan_ctx.pseudo_port_map,
        scan_ctx.manifest,
        scan_ctx.wbr_decoupled_bits,
        blackbox_instances=blackbox_instances,
    )
    if twin_map.keys() != execution_map.keys():
        raise ScanError(f"{what} maps different fault sites than the scan ATPG view")
    return _Twin(str(twin_path), twin_map)


def _build_hold_twin(
    core: Any,
    scan_ctx: ScanPipelineContext,
    reduced_json_path: str,
    generic_cell_map: str,
    reduced_cell_map: str,
    unsupported: str,
    blackbox_instances: list[str],
    execution_map: dict[str, int],
) -> _Twin | None:
    """None unless the view models non-scan cells. Blackboxes stay opaque in the
    hold twin, so what their unknown outputs reach stays unobserved: the mask is
    recomputed for them alone, the non-scan flops no longer being unknown."""
    if scan_ctx.nonscan is None:
        return None
    reduced = Path(reduced_json_path)
    view = json.loads(reduced.read_text(encoding="utf-8"))
    top = str(scan_ctx.manifest["top"])
    if not make_nonscan_free(view, top):
        return None
    twin_path = reduced.with_name(f"{reduced.stem}_holdfree.json")
    _, module = _top_module(view, top)
    module.get("attributes", {}).pop(UNOBSERVED_NETS_ATTR, None)
    x_instances = scan_ctx.cfg.blackbox_x_instances
    if x_instances:
        twin_path.write_text(json.dumps(view) + "\n", encoding="utf-8")
        mask = compute_x_mask(
            core,
            twin_path,
            top=top,
            cell_map=reduced_cell_map,
            unsupported=unsupported,
            x_instances=x_instances,
            launch_mode=scan_ctx.x_mask.launch_mode,
            functional_output_order=(),
        )
        apply_x_mask(view, top, mask)
    return _mapped_twin(
        core,
        scan_ctx,
        view,
        twin_path,
        generic_cell_map,
        reduced_cell_map,
        unsupported,
        blackbox_instances,
        execution_map,
        "hold twin",
    )


def _build_blackbox_twin(
    core: Any,
    scan_ctx: ScanPipelineContext,
    reduced_json_path: str,
    generic_cell_map: str,
    reduced_cell_map: str,
    unsupported: str,
    blackbox_instances: list[str],
    execution_map: dict[str, int],
) -> _Twin | None:
    """None unless the view models a blackbox. Non-scan cells are set free in it
    too (make_nonscan_free): tried after the hold twin, it catches a fault only a
    blackbox and the holds together block."""
    if not blackbox_instances:
        return None
    reduced = Path(reduced_json_path)
    view = json.loads(reduced.read_text(encoding="utf-8"))
    top = str(scan_ctx.manifest["top"])
    if scan_ctx.nonscan is not None:
        make_nonscan_free(view, top)
    if not make_blackbox_transparent(view, top):
        return None
    return _mapped_twin(
        core,
        scan_ctx,
        view,
        reduced.with_name(f"{reduced.stem}_bbtransparent.json"),
        generic_cell_map,
        reduced_cell_map,
        unsupported,
        blackbox_instances,
        execution_map,
        "blackbox-transparent view",
    )


def _testable_on_twin(
    core: Any,
    twin: _Twin,
    *,
    db_path: str,
    fault_id: int,
    site_key: str,
    cell_map: str,
    conflict_limit: int,
    timeout: int,
    unsupported: str,
    cone_restrict: bool,
    transition: bool,
    los: bool,
    los_couple_ports: list[tuple[str, str]],
    los_head_ports: list[str],
) -> bool:
    """Re-solve a fault that is UNSAT in the scan ATPG view on a twin of it,
    where what the view ties is free and what it leaves unread observed: False
    only if it is UNSAT there too. SAT means only what the twin frees could test
    it: the holds (Tessent's AU.PC), or a blackbox (AU.BB). TIMEOUT and UNKNOWN
    also return True -- whether such a fault is redundant is unknown, a
    redundant verdict needs a proof, and the fault is untestable in the scan
    view either way."""
    index = twin.net_index_by_site[site_key]
    if los:
        solved = core.solve_scan_los_transition_fault_atpg(
            twin.json_path,
            cell_map,
            db_path,
            fault_id,
            los_couple_ports,
            los_head_ports,
            [],
            conflict_limit,
            timeout,
            unsupported,
            cone_restrict=cone_restrict,
            net_index_override=index,
        )
    elif transition:
        solved = core.solve_scan_transition_fault_atpg(
            twin.json_path,
            cell_map,
            db_path,
            fault_id,
            [],
            conflict_limit,
            timeout,
            unsupported,
            cone_restrict=cone_restrict,
            net_index_override=index,
        )
    else:
        solved = core.solve_fault_atpg(
            twin.json_path,
            cell_map,
            db_path,
            fault_id,
            [],
            conflict_limit,
            timeout,
            unsupported,
            net_index_override=index,
        )
    return str(solved["result"]) != "UNSAT"


def _fault_has_raw_scan_diff(diff_unload_seqs: dict[int, list[bool]]) -> bool:
    """True iff a fault's faulty/golden unload sequences differ at ANY
    (chain, cycle) at all -- i.e. it was actually observed via the real
    scan-chain unload path, not solely via a functional PO the compactor
    never touches. A PO-only detection is always compaction-safe regardless
    of what _compaction_diff_observable's fold would compute on an all-zero
    map -- there is nothing to fold, so it should never be flagged."""
    return any(any(bits) for bits in diff_unload_seqs.values())


def _compaction_diff_observable(
    diff_unload_seqs: dict[int, list[bool]],
    fanout: list[list[int]],
    max_chain_length: int,
    unload_mask: dict[int, list[bool]] | None = None,
) -> bool:
    """Pure GF(2) evaluation, no simulation, no C++ call, no solve (there is
    nothing to search for -- the diff is already known). True iff at least
    one compacted output bit (fanout[o] = chain indices XORed into output o)
    differs from golden at at least one cycle. A compacted bit that folds in
    a chain bit `unload_mask` leaves don't-care is itself unknown -- the XOR
    of an unknown value -- so it can't show a difference."""
    mask = unload_mask or {}

    def known(chain_id: int, cycle: int) -> bool:
        bits = mask.get(chain_id)
        return bits is None or cycle >= len(bits) or bits[cycle]

    for cycle in range(max_chain_length):
        for row in fanout:
            if not all(known(chain_id, cycle) for chain_id in row):
                continue
            bit = False
            for chain_id in row:
                bits = diff_unload_seqs.get(chain_id)
                if bits and cycle < len(bits) and bits[cycle]:
                    bit = not bit
            if bit:
                return True
    return False


def _check_compaction_distinguishable(
    core: Any,
    scan_ctx: ScanPipelineContext,
    scan_pattern: ScanPattern,
    generic_cell_map: str,
    unsupported: str,
    is_loc: bool,
    is_los: bool,
    head_bits: dict[int, bool],
    active_clock_ports: list[str] | None,
    active_rows: list[_FaultRow],
    generic_site_index: dict[str, int],
    passed_fault_ids: list[int],
) -> set[int]:
    """Return the subset of ``passed_fault_ids`` that remain observable
    through ``scan_ctx.compaction_map``'s static XOR-tree compactor -- i.e.
    NOT compaction-indistinguishable.

    Compaction distinguishability is a per-fault property: the compactor only
    affects what a tester can observe at read-out, never what gets
    loaded/captured, so the candidate vector stays perfectly valid regardless
    -- only the specific faults whose diff happens to alias to zero lose
    credit from this witness. The check is NOT scoped to ``source == "sat"``
    -- a random-fill candidate's incidental detections are exactly as subject
    to real compacted-output aliasing as a SAT-targeted candidate's, so
    skipping random-fill here would silently over-credit coverage a real
    compacted tester could never see.

    One batched ``simulate_scan_protocol_faults(..., capture_diffs=True)``
    call over ALL of ``passed_fault_ids`` -- required even for
    reduced-trusted-graded faults, which never call the protocol simulator on
    their own path, so their diff data doesn't exist yet from any earlier
    call in this candidate's processing. A ``fault_site_key`` missing from
    ``generic_site_index`` passes through as observable (under-flag, never
    false-exclude).
    """
    assert scan_ctx.compaction_map is not None
    row_by_id = {row.fault_id: row for row in active_rows}
    max_chain_length = int(scan_ctx.manifest.get("max_chain_length", 0))
    base_kwargs = _protocol_fault_sim_kwargs(scan_ctx, scan_pattern)

    ids: list[int] = []
    specs: list[tuple[int, int]] = []
    for fault_id in passed_fault_ids:
        row = row_by_id.get(fault_id)
        if row is None:
            continue
        cidx = generic_site_index.get(row.fault_site_key)
        if cidx is None:
            continue
        ids.append(fault_id)
        specs.append((cidx, fault_type_to_sa_code(row.fault_type)))

    result = dict(
        core.simulate_scan_protocol_faults(
            str(scan_ctx.generic_json),
            generic_cell_map,
            faults=specs,
            unsupported_policy=unsupported,
            loc_two_capture=is_loc,
            los_two_capture=is_los,
            los_launch_scan_in=head_bits,
            active_clock_ports=active_clock_ports or [],
            capture_diffs=True,
            **base_kwargs,
        )
    )

    covered: set[int] = set()
    observable: set[int] = set()
    fanout = scan_ctx.compaction_map.fanout
    for batch in result.get("batches", []):
        for lane in batch.get("lanes", []):
            lane_dict = dict(lane)
            idx = lane_dict.get("fault_index")
            if idx is None or not (0 <= int(idx) < len(ids)):
                continue
            fault_id = ids[int(idx)]
            covered.add(fault_id)
            diff_unload_seqs = {
                int(chain_id): list(bits)
                for chain_id, bits in dict(
                    lane_dict.get("diff_unload_seqs", {})
                ).items()
            }
            if not _fault_has_raw_scan_diff(diff_unload_seqs) or (
                _compaction_diff_observable(
                    diff_unload_seqs,
                    fanout,
                    max_chain_length,
                    scan_pattern.unload_mask,
                )
            ):
                observable.add(fault_id)

    # A fault_site_key with no generic_site_index entry never entered `ids`,
    # so it never appears in `covered` either -- pass it through as
    # observable (under-flag, never false-exclude).
    return observable | (set(passed_fault_ids) - covered)


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
    # X->0 don't-care fill: scan ATPG leaves PIs that are don't-cares for this
    # candidate unassigned (e.g. a core's wide irq bus that no scan-tested fault
    # observes), but the golden gate -- and every downstream consumer (compaction,
    # iverilog verify, the applied test) -- requires every PI driven every cycle.
    # Pin unassigned reduced-view PIs to 0 here so the golden outputs are computed
    # under the same assignment the applied vector uses, and so the materialized
    # vector carried downstream is fully specified.
    filled = dict(vector)
    for name in input_order:
        filled.setdefault(name, False)
    samples = list(
        core.fault_free_outputs(
            reduced_json_path,
            reduced_cell_map,
            [filled],
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
    materialized = dict(filled)
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
    # Hold async set/reset PIs at their inactive level for the whole scan test.
    # The reduced view now MODELS the async control at capture (atpg_view inserts
    # `control_active ? control_value : D` before each async FF's PPO), so the hold
    # is no longer required for golden-gate consistency. We keep it as the default
    # (conventional scan: control-tree faults excluded, never activated), and drop
    # it only when the user opts into grading reset/set faults — then the SAT is
    # free to activate the control and the modeled mux makes the control-line fault
    # observable at a PPO (implication-based detection). No-op for designs without
    # scannable async-reset/set FFs.
    if scan_ctx.reset_pi_holds and not scan_ctx.cfg.fault_model.include_reset_faults:
        vector = {
            **vector,
            **{k: v for k, v in scan_ctx.reset_pi_holds.items() if k in vector},
        }
    # X->0 fill don't-care PIs once, at the candidate's entry. Scan ATPG leaves PIs
    # that the target fault neither controls nor observes unassigned (e.g. a core's
    # wide irq bus), but every downstream consumer -- the reduced golden gate, the
    # candidate verify, the tentative grade, dynamic compaction, and the serialized
    # stored pattern -- requires a fully specified vector. Pinning unassigned reduced
    # PIs to 0 here (rather than per-consumer) is the applied test's actual value and
    # keeps all consumers consistent. Without it, the strict C++ converters abort the
    # whole campaign with "missing PI in vector" on any real core.
    vector = {pi: bool(vector.get(pi, False)) for pi in input_order}
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

    scan_pattern = _serialize(scan_ctx, serialize_input)
    if scan_ctx.compression_map is not None:
        # What the chip shifts in: the gate, the protocol sims and the export
        # all see the decompressor's whole stream for the candidate's seed.
        scan_pattern = _decompressed(
            core, scan_ctx, vector, scan_pattern, head_bits if is_los else {}
        )
    if transition:
        # What a tester applies between the load and the capture: a launch pulse
        # (LOC), or one more shift with these scan-in bits (LOS), every chain's.
        chains = range(len(scan_ctx.manifest.get("scan_inputs", [])))
        scan_pattern = replace(
            scan_pattern,
            launch=launch_mode,
            launch_scan_in=(
                {chain: bool(head_bits.get(chain, False)) for chain in chains}
                if is_los
                else {}
            ),
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
        gate_scan_pattern = replace(
            scan_pattern,
            capture_pi_values={
                _rename.get(k, k): v for k, v in scan_pattern.capture_pi_values.items()
            },
        )
    else:
        gate_scan_pattern = scan_pattern
    observed_outputs = _observed_outputs(scan_ctx)
    gate_output_order = (
        [_obs_map.get(p, p) for p in observed_outputs] if _obs_map else observed_outputs
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
        (row.fault_id, row.net_index, fault_type_to_sa_code(row.fault_type))
        for row in active_rows
    ]
    sim_threads = resolve_sim_threads(scan_ctx.cfg.simulation.sim_threads)
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
                sim_threads=sim_threads,
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
                sim_threads=sim_threads,
            )
        )
    # The reduced pseudo-PI/PO view grades a fault exactly as the full
    # load+capture+unload scan protocol would: the shift is a fault-INDEPENDENT
    # permutation, and the per-candidate golden gate above
    # (reduced_protocol_matches) proves fault-free reduced==protocol on every
    # unload bit + functional PO. This holds for FF Q-stem faults too -- each FF's
    # Q net is rewired to a PPI whose stuck-at site maps to that PPI's compiled
    # index (scan.site_resolution), so a Q-stem fault propagates through Q's
    # fanout to the PPOs/POs and is observed by the reduced sim like any logic-
    # cone fault. Stuck-at therefore TRUSTS those reduced detections directly.
    # Transition Q-stem detection depends on the two-frame launch/capture
    # transition rather than a single captured value, so for transition the
    # reduced tentative is NOT authoritative and every active Q-stem fault is
    # graded by the two-frame protocol sim below.
    qstem_active = set(_active_q_stem_fault_ids(active_rows, scan_ctx.q_stem_site_keys))
    if transition:
        reduced_trusted = sorted(set(tentative) - qstem_active)
        protocol_sim_fault_ids = sorted(qstem_active)
    else:
        reduced_trusted = sorted(set(tentative))
        # Residual = Q-stem faults the reduced sim did not functionally observe;
        # graded below by the capture-value chain-integrity credit, then a
        # protocol-sim fallback for the SAT target only.
        protocol_sim_fault_ids = sorted(qstem_active - set(tentative))

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

    # Faults observed in the reduced view are trusted directly from the cheap grade.
    passed_fault_ids: list[int] = list(reduced_trusted)
    protocol_sim_rejections: list[CandidateRejection] = []
    blocked: list[tuple[int, str]] = []

    # Q-stem faults the reduced sim did not catch. Stuck-at: credit the scan-unload
    # chain-integrity case from the capture value (a stuck Q corrupts its own
    # unloaded bit, so SA0 is detected iff captured D=1 and SA1 iff captured D=0),
    # then fall back to the full protocol sim for the SAT target only when it is
    # still unresolved. Transition: full two-frame protocol sim for all.
    if protocol_sim_fault_ids:
        if not transition:
            row_by_id = {row.fault_id: row for row in active_rows}
            # Left to the protocol sim: the SAT target when its capture value
            # doesn't credit it, and every Q-stem of a flop that captures a
            # blackbox output's unknown value. That flop's own unload bit is
            # masked, but a stuck Q still corrupts each upstream bit it shifts
            # out, and the protocol sim compares those.
            protocol_ids: list[int] = []
            for fault_id in protocol_sim_fault_ids:
                row = row_by_id.get(fault_id)
                if row is None:
                    continue
                ppo_port = scan_ctx.q_stem_to_ppo.get(row.fault_site_key)
                if ppo_port is None:
                    continue
                if ppo_port in scan_ctx.x_mask.ppo_ports:
                    protocol_ids.append(fault_id)
                    continue
                cap_val = bool(reduced_expectation.get(ppo_port, False))
                detected = (
                    cap_val
                    if fault_type_to_sa_code(row.fault_type) == 0
                    else not cap_val
                )
                if detected:
                    passed_fault_ids.append(fault_id)
                elif source == "sat" and fault_id == sat_target_fault_id:
                    protocol_ids.append(fault_id)
            if protocol_ids:
                specs: list[tuple[int, int]] = []
                for fault_id in protocol_ids:
                    row = row_by_id[fault_id]
                    generic_cidx = generic_site_index.get(row.fault_site_key)
                    if generic_cidx is None:
                        raise RunnerError(
                            "generic compiled index missing for site "
                            f"{row.fault_site_key}"
                        )
                    specs.append((generic_cidx, fault_type_to_sa_code(row.fault_type)))
                protocol_fault_sim_kwargs = _protocol_fault_sim_kwargs(
                    scan_ctx, scan_pattern
                )
                protocol_fault_sim_result = dict(
                    core.simulate_scan_protocol_faults(
                        str(scan_ctx.generic_json),
                        generic_cell_map,
                        faults=specs,
                        unsupported_policy=unsupported,
                        loc_two_capture=is_loc,
                        los_two_capture=is_los,
                        los_launch_scan_in=head_bits,
                        active_clock_ports=active_clock_ports or [],
                        **protocol_fault_sim_kwargs,
                    )
                )
                outcome_by_index = {
                    int(dict(lane)["fault_index"]): str(dict(lane).get("outcome"))
                    for batch in protocol_fault_sim_result.get("batches", [])
                    for lane in batch.get("lanes", [])
                }
                for index, fault_id in enumerate(protocol_ids):
                    if outcome_by_index.get(index) == "pass":
                        passed_fault_ids.append(fault_id)
                    elif source == "sat" and fault_id == sat_target_fault_id:
                        protocol_sim_rejections.append(
                            CandidateRejection(fault_id, "no_capture_or_unload_effect")
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
                if not _launch_known(scan_ctx, row):
                    if source == "sat" and fault_id == sat_target_fault_id:
                        protocol_sim_rejections.append(
                            CandidateRejection(fault_id, "no_capture_or_unload_effect")
                        )
                        blocked.append((fault_id, track_key))
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
                    sim_threads=resolve_sim_threads(
                        scan_ctx.cfg.simulation.sim_threads
                    ),
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

    if scan_ctx.compaction_map is not None and passed_fault_ids:
        observable_ids = _check_compaction_distinguishable(
            core,
            scan_ctx,
            scan_pattern,
            generic_cell_map,
            unsupported,
            is_loc,
            is_los,
            head_bits,
            active_clock_ports,
            active_rows,
            generic_site_index,
            passed_fault_ids,
        )
        indistinguishable = [
            fid for fid in passed_fault_ids if fid not in observable_ids
        ]
        if indistinguishable:
            _reject_compaction_indistinguishable(
                indistinguishable, track_key, protocol_sim_rejections, blocked
            )
            passed_fault_ids = [
                fid for fid in passed_fault_ids if fid in observable_ids
            ]

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


def _guard_transition_on_scan_wbr(
    wbr_model: str, manifest: dict[str, Any], transition: bool
) -> None:
    """Reject transition (LOC/LOS) ATPG only on a design that ACTUALLY carries
    scan WBR cells.

    The 1-FF scan WBC delivers boundary stimulus for exactly one capture cycle; a
    second capture clobbers q, so a scan-WBR-wrapped block cannot run transition
    ATPG until the 2-FF WBC upgrade lands. But the earlier guard fired on the
    ``wbr_model`` config alone -- which defaults to "scan" -- so it wrongly rejected
    transition ATPG on a plain, UNWRAPPED scan design that has no WBR cells at all.
    A design is scan-WBR-wrapped iff its manifest has a non-empty ``wrapper_chains``
    (empty for a plain scan design and for the buffer model).
    """
    design_has_scan_wbr = bool(manifest.get("wrapper_chains"))
    if transition and wbr_model == "scan" and design_has_scan_wbr:
        raise ScanError(
            "wbr_model='scan' does not support transition (LOC/LOS) ATPG: "
            "the 1-FF WBC delivers stimulus for exactly one capture cycle; "
            "a second capture clobbers q and corrupts boundary stimulus. "
            "Use wbr_model='buffer' for transition faults, or upgrade to a "
            "2-FF WBC first."
        )


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
    from faultflow.runner.runner import _atpg_pi_names, _load_core

    # Accepted block-level scan patterns, collected for export when
    # scan_pattern_out is set (used by SoC retargeting). One per accepted vector.
    accepted_patterns: list[ScanPattern] = []

    # S4.8 — the 1-FF WBC is correct ONLY under single-capture INTEST. Fail loudly
    # rather than silently mis-deliver on a scan-WBR-wrapped block.
    _guard_transition_on_scan_wbr(cfg.wbr_model, scan_ctx.manifest, transition)

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

    input_order = _atpg_pi_names(netlist, cfg.top)
    effective_db_path = str(db_path if db_path is not None else cfg.db_path)
    reduced_json_path = str(netlist)
    scan_cell_map = resolve_scan_cell_map(cfg)
    reduced_cell_map = str(
        cell_map_path if cell_map_path is not None else scan_cell_map
    )
    generic_cell_map = str(scan_cell_map)
    unsupported = cfg.simulation.unsupported_cells
    # Every GENERIC-netlist load needs the blackbox list: a blackbox instance's
    # cell type is usually absent from the cell map, so a load without it fails
    # outright under unsupported_cells = fail. Reduced-view loads need none --
    # build_scan_atpg_view models blackboxes opaque, so the view has none.
    bb = list(cfg.blackbox_instances)
    _require_x_mask(
        scan_ctx, reduced_json_path, launch_mode_key(transition, launch_mode)
    )
    if scan_ctx.x_mask.nets:
        log.info(
            "atpg   unknown values (blackbox outputs, frozen non-scan flops) reach "
            "%d observation points (%d scan flops, %d outputs): unobserved, "
            "don't-care in every pattern",
            len(scan_ctx.x_mask.nets),
            len(scan_ctx.x_mask.ppo_ports),
            len(scan_ctx.x_mask.outputs),
        )

    core.ensure_faults_enumerated(
        str(scan_ctx.generic_json),
        generic_cell_map,
        effective_db_path,
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        unsupported,
        bb,
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
        blackbox_instances=bb,
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
                        str(scan_ctx.generic_json), generic_cell_map, unsupported, bb
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
    # Non-scan cells: their own faults, and what only they see, are left to
    # JTAG (ff.py jtag) -- tagged before any SAT, which would otherwise call
    # them redundant -- and so are the stuck-ats that could release a flop the
    # view ties in reset. A settled flop's own faults are reset faults.
    jtag_keys: set[str] = set()
    reset_keys: set[str] = set()
    if scan_ctx.nonscan is not None:
        top_name = str(scan_ctx.manifest.get("top", cfg.top))
        _, generic_module = _top_module(
            json.loads(scan_ctx.generic_json.read_text(encoding="utf-8")), top_name
        )
        site_rows = list(
            core.list_site_keys(
                str(scan_ctx.generic_json), generic_cell_map, unsupported, bb
            )
        )
        jtag_keys = jtag_sites(
            site_rows,
            generic_module,
            json.loads(Path(generic_cell_map).read_text(encoding="utf-8")),
            scan_ctx.nonscan,
            tdo=cfg.jtag.tdo,
            blackbox_instances=bb,
        )
        reset_keys = settled_sites(site_rows, scan_ctx.nonscan)
        log.info(
            "non-scan: %d fault sites left to JTAG (excluded_jtag), %d of settled "
            "flops (excluded_reset); %d-pulse preamble",
            len(jtag_keys),
            len(reset_keys),
            scan_ctx.nonscan.preamble,
        )
    with connect(effective_db_path) as conn:
        init_schema(conn)
        apply_scan_execution_map(
            conn,
            campaign_id,
            execution_map,
            exclusions,
            jtag_sites=jtag_keys,
            jtag_faults=(
                scan_ctx.nonscan.release_faults
                if scan_ctx.nonscan is not None
                else frozenset()
            ),
            reset_sites=reset_keys,
        )
    core.invalidate_stale_redundant(effective_db_path, campaign_id, redundancy_model)
    # With blackboxes opaque and non-scan cells tied in the view, UNSAT alone
    # can't tell a redundant fault from one only lifting the holds, or a
    # blackbox, could test: the UNSAT branch re-solves on these twins, in this
    # order, which can.
    twin_args = (
        core,
        scan_ctx,
        reduced_json_path,
        generic_cell_map,
        reduced_cell_map,
        unsupported,
        bb,
        execution_map,
    )
    twins = [
        (label, twin)
        for label, twin in (
            ("hold", _build_hold_twin(*twin_args)),
            ("blackbox", _build_blackbox_twin(*twin_args)),
        )
        if twin is not None
    ]
    unresolved = {"hold": 0, "blackbox": 0, "compression": 0}

    generic_site_index = build_site_key_index(
        core, scan_ctx.generic_json, generic_cell_map, unsupported, bb
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
    # is computed per-round for only the current active set (lazy — faults detected
    # by earlier rounds never pay the BFS cost). Skipped for transition faults,
    # whose two-frame cone is not captured by the single-frame size.
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
            # Each worker gets the decompressor's seed rows once, inherited
            # through the fork, not pickled into every task.
            _executor = ProcessPoolExecutor(
                max_workers=cfg.atpg.workers,
                mp_context=get_context("fork"),
                initializer=install_seed,
                initargs=(_seed_kwargs(scan_ctx, los=los),),
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

    # OT structural reconvergence preflight (cfg.atpg.preflight, stuck-at only):
    # reconvergent-site faults are sorted last and their SAT timeout tier is
    # bumped by 1 so the short (2 s) tier is skipped -- these faults are likely
    # hard and waste the short budget. Ordering only: a reconvergent stem is no
    # redundancy proof (its faults, and each branch's, can be testable), so every
    # verdict still comes from SAT. Falls back silently if opentest is not on
    # PATH or the preflight subprocess fails.
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
                log.info("atpg   preflight: %d reconvergent stems", len(_reconv_ids))

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

    def solve(
        fault_id: int, blocked: list[str], timeout: int, *, seeded: bool
    ) -> dict[str, Any]:
        """One scan SAT solve on the view; `seeded`: through the decompressor
        (no constraint without compression)."""
        seed = _seed_kwargs(scan_ctx, los=los) if seeded else {}
        if los:
            return dict(
                core.solve_scan_los_transition_fault_atpg(
                    reduced_json_path,
                    reduced_cell_map,
                    effective_db_path,
                    fault_id,
                    los_couple_ports,
                    los_head_ports,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    timeout,
                    unsupported,
                    cone_restrict=cfg.atpg.cone_restrict,
                    **seed,
                )
            )
        if transition:
            return dict(
                core.solve_scan_transition_fault_atpg(
                    reduced_json_path,
                    reduced_cell_map,
                    effective_db_path,
                    fault_id,
                    blocked,
                    cfg.atpg.sat_conflict_limit,
                    timeout,
                    unsupported,
                    cone_restrict=cfg.atpg.cone_restrict,
                    **seed,
                )
            )
        return dict(
            core.solve_fault_atpg(
                reduced_json_path,
                reduced_cell_map,
                effective_db_path,
                fault_id,
                blocked,
                cfg.atpg.sat_conflict_limit,
                timeout,
                unsupported,
                **seed,
            )
        )

    head_chain_by_port = {port: chain for chain, port in los_head_by_chain.items()}

    def random_key(vector: dict[str, bool], heads: dict[int, bool]) -> str:
        """A random candidate's dedup/blocking key: V1, launching on shift
        followed by its launch-shift scan-in bits in head-port order (V1 ‖
        head-bits, length-compatible with LOS blocked keys)."""
        key = pattern_key(vector, input_order)
        if los:
            key += "".join(
                "1" if heads.get(head_chain_by_port[port], False) else "0"
                for port in los_head_ports
            )
        return key

    terminal = "MAX_ROUNDS"
    # Random fill runs ONCE (round 1): up to random_vectors patterns, or until
    # fault coverage crosses random_stop_coverage -- then SAT mode for the rest.
    switched_to_sat = False
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

        # Each random candidate is (V1, LOS launch-shift scan-in bits by chain).
        # Compressed, both come from a random decompressor seed; otherwise the
        # head bits are 0.
        random_batch: list[tuple[dict[str, bool], dict[int, bool]]] = []
        if not switched_to_sat:
            random_started = time.perf_counter()
            random_batch = (
                _seeded_random_vectors(
                    core, input_order, scan_ctx, cfg.atpg.random_vectors, los=los
                )
                if scan_ctx.compression_map is not None
                else [
                    (vector, {})
                    for vector in core.atpg_random_vectors(
                        input_order, cfg.atpg.random_vectors, ATPGRANDOM_SEED
                    )
                ]
            )
            atpg_seconds += time.perf_counter() - random_started
        new_random: list[tuple[dict[str, bool], dict[int, bool]]] = []
        for vector, heads in random_batch:
            key = random_key(vector, heads)
            if key in seen_patterns:
                continue
            seen_patterns.add(key)
            new_random.append((vector, heads))
        heartbeat = GradeHeartbeat(
            log,
            round_idx,
            len(new_random),
            lambda: _live_detected(effective_db_path, campaign_id),
        )
        random_denom = _coverage_denominator(effective_db_path, campaign_id)
        graded_random = 0
        for offset, (vector, heads) in enumerate(new_random):
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
                        los_head_scan_in=heads if los else None,
                        candidate_key=random_key(vector, heads) if los else None,
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
            graded_random = offset + 1
            heartbeat.tick(offset + 1)
            if _random_stop_reached(
                effective_db_path,
                campaign_id,
                random_denom,
                cfg.atpg.random_stop_coverage,
            ):
                log.info(
                    "atpg   random-fill reached %.1f%% coverage after %d/%d "
                    "vectors; switching to SAT",
                    cfg.atpg.random_stop_coverage,
                    offset + 1,
                    len(new_random),
                )
                break
        if not switched_to_sat:
            # Release the patterns of any random vectors we did not grade so SAT
            # can generate them for the remaining faults; then stay in SAT mode.
            for _v, _h in new_random[graded_random:]:
                seen_patterns.discard(random_key(_v, _h))
            switched_to_sat = True
            if cfg.atpg.random_only:
                # Deliberately incomplete "pure random" terminal mode for
                # methodology comparisons -- never dispatch SAT for whatever
                # random sampling left undetected.
                terminal = "RANDOM_ONLY"
                break

        with connect(effective_db_path) as conn:
            init_schema(conn)
            active_rows = _active_fault_rows(conn, campaign_id)
            db_blocked = load_blocked_patterns(conn, campaign_id)
            rejection_reasons = (
                load_rejection_reasons(conn, campaign_id)
                if scan_ctx.compaction_map is not None
                else {}
            )

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

        # Sort: (1) reconvergent-site faults last (no cone BFS on the full set),
        # (2) smallest cone first within each group — computed lazily for the
        # current round's active set only (faults detected in prior rounds skip it).
        if (order_faults or _reconv_ids) and active_rows:
            _round_cone_sizes: dict[int, int] = (
                _cone_size_map(
                    core,
                    reduced_json_path,
                    reduced_cell_map,
                    active_rows,
                    unsupported,
                )
                if order_faults
                else {}
            )
            active_rows = sorted(
                active_rows,
                key=lambda r: (
                    r.net_id in _reconv_ids,
                    _round_cone_sizes.get(r.fault_id, 1 << 30) if order_faults else 0,
                ),
            )

        current_active_ids = {row.fault_id for row in active_rows}

        # Parallel: pre-solve all active faults in the worker pool before the
        # processing loop.  workers==1 leaves _parallel_results empty and the
        # serial solve path below runs unchanged (A/B control).
        _parallel_results: dict[int, tuple[str, dict]] = {}
        # Heartbeat for the silent scan SAT-solve phase (parallel wave + serial
        # fallback) so a long round visibly progresses instead of looking hung.
        _solve_hb = SolveHeartbeat(
            log,
            round_idx,
            len(active_rows),
            lambda: _live_detected(effective_db_path, campaign_id),
        )
        _solved_count = 0
        # Per-fault SAT outcome (timeout/unknown) for this round's reason report.
        _sat_outcomes: dict[int, str] = {}
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
                    [],  # bb_instances: the reduced view models blackboxes opaque
                    "",  # test_mode: baked into the fused-view netlist
                    cfg.atpg.incremental_sat,  # scan_stuck_at ignores it for now
                )
                for row in _dispatch_rows
            ]
            _wave_started = time.perf_counter()
            try:
                for _fid, _res, _slv in _executor.map(solve_fault_worker, _wave_args):
                    _parallel_results[int(_fid)] = (_res, dict(_slv))
                    _solved_count += 1
                    _solve_hb.tick(_solved_count)
            except BrokenProcessPool as _exc:
                # A worker process DIED (typically OOM-killed). The pool is
                # permanently broken: swallowing this used to turn every
                # remaining fault of every remaining round into a silent
                # UNKNOWN and "complete" with garbage coverage. Fail loudly
                # with the remedy; DB state written so far is preserved.
                _executor.shutdown(wait=False, cancel_futures=True)
                raise RunnerError(
                    "parallel SAT worker process died mid-wave (likely "
                    "out-of-memory). Progress so far is saved; re-run with "
                    "fewer workers (atpg.workers) and/or "
                    "atpg.incremental_sat=false to cut per-worker memory."
                ) from _exc
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
                solved = solve(fault_id, blocked, tier_timeout, seeded=True)
                atpg_seconds += time.perf_counter() - solve_started
                result = str(solved["result"])
                _solved_count += 1
                _solve_hb.tick(_solved_count)
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
                    rejected_patterns.setdefault(fault_id, set()).add(key)
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
                q_stem = row.fault_site_key in scan_ctx.q_stem_site_keys
                # Through the decompressor UNSAT proves no redundancy: solved
                # again without it, a test (or no verdict: a redundant one
                # needs a proof) makes the fault testable, only not through
                # this decompressor -- compression_unresolved (Tessent's AU.EDT).
                testable_free = False
                if scan_ctx.compression_map is not None and not q_stem:
                    solve_started = time.perf_counter()
                    testable_free = (
                        solve(fault_id, blocked, tier_timeout, seeded=False)["result"]
                        != "UNSAT"
                    )
                    atpg_seconds += time.perf_counter() - solve_started
                if q_stem:
                    core.mark_fault_protocol_unresolved(effective_db_path, fault_id)
                elif testable_free:
                    core.mark_fault_compression_unresolved(effective_db_path, fault_id)
                    unresolved["compression"] += 1
                elif _is_compaction_only_rejected(rejection_reasons.get(fault_id)):
                    core.mark_fault_compaction_unresolved(effective_db_path, fault_id)
                else:
                    untestable = None
                    # UNSAT with patterns blocked isn't a proof to classify.
                    for label, twin in [] if blocked else twins:
                        if _testable_on_twin(
                            core,
                            twin,
                            db_path=effective_db_path,
                            fault_id=fault_id,
                            site_key=row.fault_site_key,
                            cell_map=reduced_cell_map,
                            conflict_limit=cfg.atpg.sat_conflict_limit,
                            timeout=tier_timeout,
                            unsupported=unsupported,
                            cone_restrict=cfg.atpg.cone_restrict,
                            transition=transition,
                            los=los,
                            los_couple_ports=los_couple_ports,
                            los_head_ports=los_head_ports,
                        ):
                            untestable = label
                            break
                    if untestable == "hold":
                        core.mark_fault_hold_unresolved(effective_db_path, fault_id)
                    elif untestable == "blackbox":
                        core.mark_fault_blackbox_unresolved(effective_db_path, fault_id)
                    else:
                        core.mark_fault_redundant(
                            effective_db_path, fault_id, redundancy_model
                        )
                    if untestable is not None:
                        unresolved[untestable] += 1
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
    if unresolved["hold"]:
        log.info(
            "atpg   %d faults are testable only with [scan] hold lifted "
            "(hold_unresolved)",
            unresolved["hold"],
        )
    if unresolved["blackbox"]:
        log.info(
            "atpg   %d faults are testable only through a blackbox "
            "(blackbox_unresolved)",
            unresolved["blackbox"],
        )
    if unresolved["compression"]:
        log.info(
            "atpg   %d faults are testable, but by no load the decompressor "
            "can make (compression_unresolved)",
            unresolved["compression"],
        )

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
    scan_pattern = _serialize(scan_ctx, serialize_input)
    if scan_ctx.compression_map is not None:
        scan_pattern = _decompressed(
            core, scan_ctx, vector, scan_pattern, head_bits if is_los else {}
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
            sim_threads=resolve_sim_threads(scan_ctx.cfg.simulation.sim_threads),
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
        core,
        scan_ctx.generic_json,
        generic_cell_map,
        unsupported,
        scan_ctx.cfg.blackbox_instances,
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
