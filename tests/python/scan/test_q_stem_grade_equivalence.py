"""Gold-standard cross-check of the corrected three-tier Q-stem stuck-at grade
against the real `simulate_scan_protocol_faults` oracle on a multi-FF design.

Background: the dc559ca bug graded scan-FF Q-stem stuck-at faults ONLY by the
FF's own captured-D value (self-capture / Role 3), dropping faults detectable via
Q's functional fanout (Role 1). The corrected grade is tier-1 (reduced functional
sim) ∪ tier-2 (self-capture credit) ∪ tier-3 (protocol-sim fallback for the SAT
target). This test validates it against the authoritative protocol grade, and is
deliberately precise about what equals what:

  * SOUNDNESS (Invariant 1): the cheap tiers (1∪2) never credit a fault the
    protocol rejects. This is the property that matters — an over-crediting tier
    would inflate coverage. Since tier 3 IS the protocol sim for the SAT target,
    a sound cheap grade makes the full three-tier grade equal the protocol grade
    for every fault.
  * The cheap tiers are a SUBSET of protocol, not equal to it: a Q-stem fault can
    be protocol-detected purely by scan-chain-integrity corruption (a stuck Q
    mangles OTHER FFs' bits shifting through it during unload) — neither
    functional (tier 1) nor own-bit self-capture (tier 2). Those are covered by
    the tier-3 fallback when the fault becomes the SAT target (Invariant 3).

The single_chain fixture exercises all three on one capture vector
(__ppi_ff0=1, captured __ppo_ff0=0, captured __ppo_ff1=1):
  * ff0.Q (-> u_y XOR -> Y) SA0 is Role-1-only — the exact fault the bug dropped;
  * ff0.Q SA1 is Role-3-only (own captured bit corrupted);
  * ff1.Q (no functional fanout, captured D=1) SA1 is chain-integrity-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.protocol import serialize_vector
from faultflow.scan.site_resolution import build_scan_execution_map

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "tests/fixtures/scan_protocol/single_chain"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

SA0, SA1 = 0, 1


class _FF(NamedTuple):
    instance: str
    q_net: int
    reduced_cidx: int  # Q-stem PPI index in the reduced view (tier-1 sim)
    captured_d: bool  # captured next-state at the PPO (tier-2 self-capture)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _protocol_detects(
    core: Any,
    generic_path: Path,
    load_seqs: dict[int, list[bool]],
    capture_pi_values: dict[str, bool],
    compiled_net_index: int,
    sa_code: int,
) -> bool:
    """Authoritative full load+capture+unload protocol grade for one fault."""
    result = core.simulate_scan_protocol_faults(
        str(generic_path),
        str(CELL_MAP),
        clock_ports=["CLK"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in"],
        scan_output_ports=["scan_out"],
        functional_output_ports=["Y"],
        max_chain_length=2,
        load_seqs=load_seqs,
        capture_pi_values=capture_pi_values,
        faults=[(compiled_net_index, sa_code)],
        unsupported_policy="fail",
    )
    for batch in result.get("batches", []):
        for lane in batch.get("lanes", []):
            return str(dict(lane).get("outcome")) == "pass"
    return False


@pytest.mark.golden
def test_q_stem_three_tier_equals_pure_protocol(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic = _load(FIXTURE_ROOT / "generic.json")
    manifest = _load(FIXTURE_ROOT / "manifest.json")
    case = _load(FIXTURE_ROOT / "cases.json")["cases"][0]

    generic_path = tmp_path / "generic.json"
    generic_path.write_text(json.dumps(generic, indent=2) + "\n", encoding="utf-8")
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    # Reduced compiled indices for each Q-stem site (site_key -> reduced PPI index).
    execution, _exclusions = build_scan_execution_map(
        core, generic_path, reduced_path, CELL_MAP, CELL_MAP, "fail", port_map, manifest
    )
    # Generic compiled indices for the same Q nets (for the protocol oracle).
    generic_cidx = {
        int(row["yosys_net_id"]): int(row["compiled_net_index"])
        for row in core.list_site_keys(str(generic_path), str(CELL_MAP), "fail")
        if str(row["site_key"]).endswith(":stem")
    }

    # Bridge the reduced vector to the physical protocol load + capture PIs.
    reduced_vector = dict(case["reduced_vector"])
    pattern = serialize_vector(reduced_vector, port_map, manifest)
    capture_pi_values = {name: bool(reduced_vector[name]) for name in ("A", "D0", "D1")}

    module = reduced["modules"]["scan_protocol_single"]
    input_order = [
        name for name, p in module["ports"].items() if p["direction"] == "input"
    ]
    reduced_inputs = {name: bool(reduced_vector[name]) for name in input_order}

    # Per-FF facts: q net, reduced index, captured-D (PPO) value, and which tier
    # each polarity exercises on this vector (for the explicit bug-guard asserts).
    captured = dict(case["captured_ppo"])
    ffs: list[_FF] = []
    for instance, entry in port_map.items():
        q_site = str(entry["boundary"]["q_stem_site_key"])
        ffs.append(
            _FF(
                instance=instance,
                q_net=int(str(q_site).split(":")[1]),
                reduced_cidx=int(execution[q_site]),
                captured_d=bool(captured[str(entry["ppo_port"])]),
            )
        )

    # Tier 1: one reduced fault-sim with ALL Q-stem faults preloaded; the returned
    # ids are the functionally-detected ones (Role 1).
    preloaded: list[tuple[int, int, int]] = []
    fid_to_fault: dict[int, tuple[str, int]] = {}
    fid = 0
    for ff in ffs:
        for sa in (SA0, SA1):
            preloaded.append((fid, ff.reduced_cidx, sa))
            fid_to_fault[fid] = (ff.instance, sa)
            fid += 1
    tier1_ids = set(
        int(x)
        for x in core.simulate_tentative_preloaded(
            str(reduced_path),
            str(CELL_MAP),
            preloaded,
            reduced_inputs,
            input_order,
            "fail",
        )
    )

    ff_by_instance = {ff.instance: ff for ff in ffs}
    grade: dict[tuple[str, int], dict[str, bool]] = {}
    for fid_i, (instance, sa) in fid_to_fault.items():
        ff = ff_by_instance[instance]
        tier1 = fid_i in tier1_ids
        # Tier 2 (self-capture / Role 3): SA0 detected iff captured D=1, SA1 iff D=0.
        cap = ff.captured_d
        tier2 = cap if sa == SA0 else (not cap)
        protocol = _protocol_detects(
            core,
            generic_path,
            pattern.load_seqs,
            capture_pi_values,
            generic_cidx[ff.q_net],
            sa,
        )
        grade[(instance, sa)] = {
            "tier1": tier1,
            "tier2": tier2,
            "protocol": protocol,
        }

    # ---- Invariant 1: SOUNDNESS. The cheap tiers must never credit a fault the
    # authoritative protocol sim rejects. This is the property that matters: an
    # over-crediting tier would inflate coverage. (The campaign's effective grade
    # is tier1 ∪ tier2 ∪ tier3-protocol-fallback; since tier3 IS protocol for the
    # SAT target, the full grade equals protocol for every fault exactly when the
    # cheap tiers are sound — which is what this asserts.)
    for (instance, sa), g in grade.items():
        if g["tier1"] or g["tier2"]:
            assert g["protocol"], (
                f"{instance} {'SA0' if sa == SA0 else 'SA1'} credited by a cheap "
                f"tier but NOT protocol-detected: {g}"
            )

    # ---- Invariant 2: the fix actually recovers the dc559ca-dropped fault.
    # ff0.Q SA0 is Role-1-only on this vector: loaded __ppi_ff0=1 stuck to 0 flips
    # Y (tier1 detects), while ff0's own captured D=0 so the self-capture-only
    # bypass (tier2) MISSES it. If tier 1 is ever reverted to exclude Q-stem,
    # tier1 flips False here and this assertion fails.
    assert grade[("ff0", SA0)] == {"tier1": True, "tier2": False, "protocol": True}
    # ff0.Q SA1 is the complementary Role-3-only case (own captured bit corrupted).
    assert grade[("ff0", SA1)] == {"tier1": False, "tier2": True, "protocol": True}

    # ---- Invariant 3: a chain-integrity-only fault that the cheap tiers cannot
    # see is still protocol-detected, and is therefore covered by the tier-3
    # fallback when it becomes the SAT target. ff1.Q has no functional fanout
    # (tier1 False) and its own captured D=1 (tier2 False for SA1), yet a stuck
    # ff1.Q corrupts ff0's value as it shifts through during unload -> detected.
    assert grade[("ff1", SA1)] == {"tier1": False, "tier2": False, "protocol": True}
