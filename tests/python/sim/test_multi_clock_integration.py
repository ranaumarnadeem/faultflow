"""Phase 6 multi-clock integration tests.

Design: multi_clock_2domain — 3 FFs across two clock domains (clk_a / clk_b)
with a cross-domain combinational path (q1_a feeds ff0_b.D via AND2).

pre_stitch.json  — pre-scan-insertion JSON; dfxtp_1 cells (two domains)
two_domain_3ff.json — post-stitch JSON; $scanff_faultflow cells + Sky130 logic

Test layers:
  1. Stitch: plan_scan_json on pre_stitch yields 2 domain-pure chains
  2. Rule check: CLK003 is INFO, report passes
  3. C++ simulation: simulate_scan_pattern with two clock ports
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
PRE_STITCH = ROOT / "tests/fixtures/multi_clock/pre_stitch.json"
POST_STITCH = ROOT / "tests/fixtures/multi_clock/two_domain_3ff.json"
TOP = "multi_clock_2domain"


# ---------------------------------------------------------------------------
# Layer 1 — scan stitch plan (no C++ core required)
# ---------------------------------------------------------------------------


def test_plan_scan_two_domains_gives_two_chains() -> None:
    from faultflow.scan.stitch import plan_scan_json

    plan = plan_scan_json(PRE_STITCH, CELL_MAP, TOP, scan_chains=2)
    assert plan.chain_count == 2
    assert sorted(plan.clock_nets) == [2, 3]
    assert plan.cell_count == 3


def test_plan_scan_chains_are_domain_pure() -> None:
    from faultflow.scan.stitch import plan_scan_json

    plan = plan_scan_json(PRE_STITCH, CELL_MAP, TOP, scan_chains=2)
    for chain in plan.chains:
        clock_nets = {r.clock_net for r in chain.cells}
        assert (
            len(clock_nets) == 1
        ), f"chain {chain.index} spans domains {sorted(clock_nets)}"


def test_plan_scan_domain_a_has_two_ffs() -> None:
    from faultflow.scan.stitch import plan_scan_json

    plan = plan_scan_json(PRE_STITCH, CELL_MAP, TOP, scan_chains=2)
    chain_a = next(c for c in plan.chains if any(r.clock_net == 2 for r in c.cells))
    assert len(chain_a.cells) == 2


def test_plan_scan_domain_b_has_one_ff() -> None:
    from faultflow.scan.stitch import plan_scan_json

    plan = plan_scan_json(PRE_STITCH, CELL_MAP, TOP, scan_chains=2)
    chain_b = next(c for c in plan.chains if any(r.clock_net == 3 for r in c.cells))
    assert len(chain_b.cells) == 1


def test_plan_scan_single_chain_raises_domain_error() -> None:
    from faultflow.scan.errors import ScanError
    from faultflow.scan.stitch import plan_scan_json

    with pytest.raises(ScanError, match="clock domains"):
        plan_scan_json(PRE_STITCH, CELL_MAP, TOP, scan_chains=1)


def test_stitch_scan_produces_stitched_json(tmp_path: Path) -> None:
    from faultflow.scan.stitch import stitch_scan_json

    out = tmp_path / "stitched.json"
    result = stitch_scan_json(PRE_STITCH, CELL_MAP, TOP, out, scan_chains=2)
    assert out.exists()
    assert result.chain_count == 2
    assert sorted(result.clock_nets) == [2, 3]


def test_stitch_scan_port_names_numbered_for_multiple_chains(
    tmp_path: Path,
) -> None:
    from faultflow.scan.stitch import stitch_scan_json

    out = tmp_path / "stitched.json"
    result = stitch_scan_json(PRE_STITCH, CELL_MAP, TOP, out, scan_chains=2)
    assert result.scan_inputs == ["scan_in_0", "scan_in_1"]
    assert result.scan_outputs == ["scan_out_0", "scan_out_1"]


# ---------------------------------------------------------------------------
# Layer 2 — rule check (no C++ core required)
# ---------------------------------------------------------------------------


def test_rule_check_multi_clock_is_info_not_error() -> None:
    from faultflow.rule_check.model import Severity
    from faultflow.rule_check.rules import run_rule_check

    report = run_rule_check(PRE_STITCH, CELL_MAP, TOP)
    assert (
        report.passed()
    ), f"expected no errors; got: {[v.format_line() for v in report.errors]}"
    clk003 = next((v for v in report.violations if v.rule_id == "CLK003"), None)
    assert clk003 is not None, "CLK003 should be reported for a multi-clock design"
    assert clk003.severity is Severity.INFO
    assert "2 clock domains" in clk003.message


def test_rule_check_clocks_controllable_from_pis() -> None:
    from faultflow.rule_check.rules import run_rule_check

    report = run_rule_check(PRE_STITCH, CELL_MAP, TOP)
    clk_ids = {v.rule_id for v in report.violations}
    assert "CLK001" not in clk_ids, "both clocks should be PI-controllable"


# ---------------------------------------------------------------------------
# Layer 3 — C++ simulation with two clock ports (requires compiled extension)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_simulate_scan_pattern_two_clock_ports(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    result = core.simulate_scan_pattern(
        str(POST_STITCH),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["Q0_A"],
        max_chain_length=2,
        load_seqs={0: [False, True], 1: [True]},
        capture_pi_values={"d0": False, "d1": True, "d2": False},
    )
    assert "real_po_values" in result
    assert "unload_seqs" in result


@pytest.mark.golden
def test_simulate_scan_pattern_length1_list_matches_scalar_behavior(
    require_cpp_core: None,
) -> None:
    """A 1-element clock_ports list behaves like the prior single-clock path."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    single_chain = ROOT / "tests/cpp/fixtures/tiny_scan_chain.json"
    result = core.simulate_scan_pattern(
        str(single_chain),
        str(CELL_MAP),
        ["CLK"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in"],
        scan_output_ports=["scan_out_0"],
        functional_output_ports=["Q0", "Q1"],
        max_chain_length=3,
        load_seqs={0: [True, False, True]},
        capture_pi_values={"D0": True, "D1": False, "D2": True},
    )
    assert result["real_po_values"] is not None
    assert 0 in result["unload_seqs"]


@pytest.mark.golden
def test_fault_sim_two_clock_ports_detects_faults(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    site_rows = core.list_site_keys(str(POST_STITCH), str(CELL_MAP), "fail")
    assert len(site_rows) > 0, "should have fault sites in the multi-clock design"

    d0_idx = site_rows[0]["compiled_net_index"]
    result = core.simulate_scan_protocol_faults(
        str(POST_STITCH),
        str(CELL_MAP),
        ["clk_a", "clk_b"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in_a", "scan_in_b"],
        scan_output_ports=["scan_out_a", "scan_out_b"],
        functional_output_ports=["Q0_A"],
        max_chain_length=2,
        load_seqs={0: [False, True], 1: [True]},
        capture_pi_values={"d0": False, "d1": True, "d2": False},
        faults=[(d0_idx, 0), (d0_idx, 1)],
    )
    assert "batches" in result
    assert len(result["batches"]) > 0
