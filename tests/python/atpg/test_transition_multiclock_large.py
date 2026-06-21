"""Phase 6b — larger multi-clock transition validation (6B-V04, 6B-V05).

Design: two_clk_seq (2-domain, clk_a / clk_b)
  clk_a domain: ff_a0 (D=d0, Q=qa0), ff_a1 (D=xor_out, Q=qa1)
    intra-A path: qa0 → u_xor_a(XOR d1) → xor_out → ff_a1.D
  clk_b domain: ff_b0 (D=d2, Q=qb0), ff_b1 (D=and_out_b, Q=qb1), ff_b2 (D=cross_and_out)
    intra-B path: qb0 → u_and_b(AND d3) → and_out_b → ff_b1.D
  cross-domain: qa1 → u_cross_and(AND d2) → cross_and_out(net16) → ff_b2.D

Net IDs:
  2=clk_a, 3=clk_b, 4=rst_n, 5=d0, 6=d1, 7=d2, 8=d3
  10=qa0, 11=xor_out, 12=qa1(q_out_a), 13=qb0, 14=and_out_b, 15=qb1(q_out_b)
  16=cross_and_out, 17=qb2(scan_out_b)
  20=scan_en, 21=scan_in_a, 22=scan_in_b
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
PRE_STITCH = ROOT / "tests/fixtures/multi_clock/two_clk_seq_pre_stitch.json"
STITCHED = ROOT / "tests/fixtures/multi_clock/two_clk_seq_stitched.json"
TOP = "two_clk_seq"

_MANIFEST: dict[str, Any] = {
    "top": TOP,
    "clock_nets": [2, 3],
    "scan_enable": "scan_en",
    "scan_inputs": ["scan_in_a", "scan_in_b"],
    "scan_outputs": ["scan_out_a", "scan_out_b"],
    "max_chain_length": 3,
    "cells": [
        {
            "instance": "ff_a0",
            "chain_index": 0,
            "chain_position": 0,
            "clock_net": 2,
            "q_net": 10,
            "data_net": 5,
            "scan_in_net": 21,
            "scan_enable_net": 20,
        },
        {
            "instance": "ff_a1",
            "chain_index": 0,
            "chain_position": 1,
            "clock_net": 2,
            "q_net": 12,
            "data_net": 11,
            "scan_in_net": 10,
            "scan_enable_net": 20,
        },
        {
            "instance": "ff_b0",
            "chain_index": 1,
            "chain_position": 0,
            "clock_net": 3,
            "q_net": 13,
            "data_net": 7,
            "scan_in_net": 22,
            "scan_enable_net": 20,
        },
        {
            "instance": "ff_b1",
            "chain_index": 1,
            "chain_position": 1,
            "clock_net": 3,
            "q_net": 15,
            "data_net": 14,
            "scan_in_net": 13,
            "scan_enable_net": 20,
        },
        {
            "instance": "ff_b2",
            "chain_index": 1,
            "chain_position": 2,
            "clock_net": 3,
            "q_net": 17,
            "data_net": 16,
            "scan_in_net": 15,
            "scan_enable_net": 20,
        },
    ],
}


# ---------------------------------------------------------------------------
# 6B-V04 — cross-domain classifier on the larger design
# ---------------------------------------------------------------------------


def test_large_design_cross_domain_classifier_net16() -> None:
    """6B-V04a: net 16 (cross_and_out) is exclusively cross-domain."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(STITCHED, _MANIFEST)
    assert (
        16 in cross
    ), f"expected net 16 (cross_and_out) in cross-domain set; got {sorted(cross)}"


def test_large_design_intra_a_nets_not_excluded() -> None:
    """6B-V04b: intra-domain-A nets are testable (not cross-domain)."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(STITCHED, _MANIFEST)
    # net 10 (qa0): domain-A launch, captured at ff_a1.D (intra-A)
    # net 11 (xor_out): domain-A launch via qa0, captured at ff_a1.D
    # net 12 (qa1): captured at PO q_out_a
    for net_id in (10, 11, 12):
        assert (
            net_id not in cross
        ), f"intra-A net {net_id} should be testable; found in cross-domain set"


def test_large_design_intra_b_nets_not_excluded() -> None:
    """6B-V04c: intra-domain-B nets are testable (not cross-domain)."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(STITCHED, _MANIFEST)
    # net 13 (qb0): domain-B launch, captured at ff_b1.D (intra-B)
    # net 14 (and_out_b): domain-B launch via qb0, captured at ff_b1.D
    # net 15 (qb1): captured at PO q_out_b
    # net 17 (qb2): captured at PO scan_out_b
    for net_id in (13, 14, 15, 17):
        assert (
            net_id not in cross
        ), f"intra-B net {net_id} should be testable; found in cross-domain set"


def test_large_design_only_net16_cross_domain() -> None:
    """6B-V04d: exactly net 16 is cross-domain (no false positives)."""
    from faultflow.scan.domain_reach import compute_cross_domain_net_ids

    cross = compute_cross_domain_net_ids(STITCHED, _MANIFEST)
    assert cross == {16}, f"expected only {{16}} as cross-domain; got {sorted(cross)}"


# ---------------------------------------------------------------------------
# 6B-V05 — per-domain C++ staggered LOC simulation
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_large_clk_a_only_all_domain_b_hold(require_cpp_core: None) -> None:
    """6B-V05a: clk_a-only LOC — all 3 domain-B FFs hold loaded True values.

    Load chain B: all True → ff_b0.Q=True, ff_b1.Q=True, ff_b2.Q=True.
    clk_b does not fire during LOC → all 3 hold True.
    Unload chain B (max_chain_length=3): [ff_b2.Q, ff_b1.Q, ff_b0.Q] = [T, T, T].
    """
    import _faultflow_core as core  # type: ignore[import-not-found]

    result = core.simulate_scan_pattern(
        str(STITCHED),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["q_out_a", "q_out_b"],
        max_chain_length=3,
        load_seqs={0: [False, False, False], 1: [True, True, True]},
        capture_pi_values={"d0": False, "d1": False, "d2": False, "d3": False},
        active_clock_ports=["clk_a"],
        loc_two_capture=True,
    )
    unload = {int(k): v for k, v in result["unload_seqs"].items()}
    for i in range(3):
        assert (
            unload[1][i] is True
        ), f"domain-B FF at unload[1][{i}] should hold True; got {unload[1]}"


@pytest.mark.golden
def test_large_clk_b_only_all_domain_a_hold(require_cpp_core: None) -> None:
    """6B-V05b: clk_b-only LOC — both domain-A FFs hold loaded True values.

    Load chain A: all True → ff_a0.Q=True, ff_a1.Q=True.
    clk_a does not fire → both hold True.
    Unload chain A (3 pulses): [ff_a1.Q=True, ff_a0.Q=True, X].
    """
    import _faultflow_core as core  # type: ignore[import-not-found]

    result = core.simulate_scan_pattern(
        str(STITCHED),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["q_out_a", "q_out_b"],
        max_chain_length=3,
        load_seqs={0: [True, True, True], 1: [False, False, False]},
        capture_pi_values={"d0": False, "d1": False, "d2": False, "d3": False},
        active_clock_ports=["clk_b"],
        loc_two_capture=True,
    )
    unload = {int(k): v for k, v in result["unload_seqs"].items()}
    assert (
        unload[0][0] is True
    ), f"ff_a1.Q should hold True during clk_b-only LOC; unload_a={unload[0]}"
    assert (
        unload[0][1] is True
    ), f"ff_a0.Q should hold True during clk_b-only LOC; unload_a={unload[0]}"


@pytest.mark.golden
def test_large_clk_a_only_intra_a_captures(require_cpp_core: None) -> None:
    """6B-V05c: clk_a-only LOC captures intra-A XOR transition into ff_a1.

    Load: all FFs=False (qa0=0, qa1=0, etc.).
    d1=True: xor_out = qa0 XOR d1 = 0 XOR 1 = 1.
    LOC clk_a: ff_a1 captures 1. ff_a0 captures d0=False (stays 0).
    xor_out stays 1 in the second capture cycle too (fa0 stays 0, d1 still True).
    Unload chain A (3 pulses): [ff_a1.Q=True, ff_a0.Q=False, X].
    """
    import _faultflow_core as core  # type: ignore[import-not-found]

    result = core.simulate_scan_pattern(
        str(STITCHED),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["q_out_a", "q_out_b"],
        max_chain_length=3,
        load_seqs={0: [False, False, False], 1: [False, False, False]},
        capture_pi_values={"d0": False, "d1": True, "d2": False, "d3": False},
        active_clock_ports=["clk_a"],
        loc_two_capture=True,
    )
    unload = {int(k): v for k, v in result["unload_seqs"].items()}
    assert (
        unload[0][0] is True
    ), f"ff_a1.Q should be True after clk_a-only LOC; unload_a={unload[0]}"
