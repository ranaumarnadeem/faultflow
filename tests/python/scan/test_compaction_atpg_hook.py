"""Unit tests for the compaction check-and-retry hook in
faultflow.scan.detection_pipeline -- proves the pure GF(2) fold, the
per-fault pruning wiring (using a stubbed simulate_scan_protocol_faults
result), and the rejection/blocked-pattern bookkeeping, without needing a
real end-to-end SAT run. Mirrors test_compression_atpg_hook.py's structure."""

from __future__ import annotations

import pytest

import faultflow.scan.detection_pipeline as dp
from faultflow.scan.compaction import CompactionMap
from faultflow.scan.detection_pipeline import (
    ScanPipelineContext,
    _FaultRow,
    _check_compaction_distinguishable,
    _compaction_diff_observable,
    _fault_has_raw_scan_diff,
    _is_compaction_only_rejected,
    _reject_compaction_indistinguishable,
)


def _ctx(compaction_map) -> ScanPipelineContext:
    return ScanPipelineContext(
        cfg=None,  # not read by _check_compaction_distinguishable itself
        manifest={"max_chain_length": 2},
        generic_json="generic.json",
        pseudo_port_map={},
        functional_output_order=[],
        q_stem_site_keys=frozenset(),
        compaction_map=compaction_map,
    )


class _FakeCore:
    """Stubs simulate_scan_protocol_faults, returning a canned
    diff_unload_seqs per lane (keyed by position in the `faults` list this
    call receives, matching real ordering)."""

    def __init__(self, lane_diffs: list[dict[int, list[bool]]]) -> None:
        self.lane_diffs = lane_diffs
        self.calls: list[tuple[list, bool | None]] = []

    def simulate_scan_protocol_faults(self, *args, faults, **kwargs):
        self.calls.append((faults, kwargs.get("capture_diffs")))
        lanes = [
            {
                "fault_index": i,
                "outcome": "pass",
                "diff_unload_seqs": (
                    self.lane_diffs[i] if i < len(self.lane_diffs) else {}
                ),
            }
            for i in range(len(faults))
        ]
        return {
            "golden_real_po_values": {},
            "golden_unload_seqs": {},
            "batches": [{"batch_index": 0, "lanes": lanes}],
        }


@pytest.fixture
def _stub_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dp,
        "_protocol_fault_sim_kwargs",
        lambda ctx, pattern: {
            "clock_ports": ["clk"],
            "scan_enable_port": "scan_en",
            "scan_input_ports": ["scan_in_0"],
            "scan_output_ports": ["scan_out_0", "scan_out_1"],
            "functional_output_ports": [],
            "max_chain_length": 2,
            "load_seqs": {},
            "capture_pi_values": {},
        },
    )


@pytest.mark.unit
def test_compaction_diff_observable_single_chain_differing() -> None:
    assert (
        _compaction_diff_observable({0: [True, False]}, [[0]], max_chain_length=2)
        is True
    )


@pytest.mark.unit
def test_compaction_diff_observable_cancels_when_identical_every_cycle() -> None:
    diff = {0: [True, True], 1: [True, True]}
    assert _compaction_diff_observable(diff, [[0, 1]], max_chain_length=2) is False


@pytest.mark.unit
def test_compaction_diff_observable_true_when_cycles_dont_coincide() -> None:
    diff = {0: [True, False], 1: [False, True]}
    assert _compaction_diff_observable(diff, [[0, 1]], max_chain_length=2) is True


@pytest.mark.unit
def test_compaction_diff_observable_false_for_all_zero_diff() -> None:
    assert (
        _compaction_diff_observable({0: [False, False]}, [[0]], max_chain_length=2)
        is False
    )


@pytest.mark.unit
def test_fault_has_raw_scan_diff_false_for_all_false_or_empty() -> None:
    assert _fault_has_raw_scan_diff({}) is False
    assert _fault_has_raw_scan_diff({0: [False, False], 1: [False]}) is False


@pytest.mark.unit
def test_fault_has_raw_scan_diff_true_for_any_true() -> None:
    assert _fault_has_raw_scan_diff({0: [False, True]}) is True


@pytest.mark.unit
def test_check_compaction_distinguishable_all_observable(
    _stub_kwargs: None,
) -> None:
    compaction_map = CompactionMap(1, [[0, 1]])
    ctx = _ctx(compaction_map)
    core = _FakeCore(
        lane_diffs=[
            {0: [True, False], 1: [False, False]},
            {0: [False, True], 1: [False, False]},
        ]
    )
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
        _FaultRow(fault_id=2, fault_site_key="s2", fault_type="sa0", net_index=1),
    ]

    observable = _check_compaction_distinguishable(
        core,
        ctx,
        scan_pattern=object(),
        generic_cell_map="generic_cell_map.json",
        unsupported="fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={"s1": 0, "s2": 1},
        passed_fault_ids=[1, 2],
    )

    assert observable == {1, 2}
    assert core.calls and core.calls[0][1] is True  # capture_diffs=True passed


@pytest.mark.unit
def test_check_compaction_distinguishable_po_only_diff_always_included(
    _stub_kwargs: None,
) -> None:
    # An all-empty diff map means "no scan-chain diff at all" (PO-only
    # detection) -- always compaction-safe, regardless of fanout.
    compaction_map = CompactionMap(1, [[0, 1]])
    ctx = _ctx(compaction_map)
    core = _FakeCore(lane_diffs=[{}])
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
    ]

    observable = _check_compaction_distinguishable(
        core,
        ctx,
        scan_pattern=object(),
        generic_cell_map="generic_cell_map.json",
        unsupported="fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={"s1": 0},
        passed_fault_ids=[1],
    )

    assert observable == {1}


@pytest.mark.unit
def test_check_compaction_distinguishable_excludes_aliased_fault(
    _stub_kwargs: None,
) -> None:
    # Both chains differ identically every cycle -> cancels under fanout
    # [[0, 1]] -> a real (compacted) diff of nonzero-but-folds-to-zero.
    compaction_map = CompactionMap(1, [[0, 1]])
    ctx = _ctx(compaction_map)
    core = _FakeCore(lane_diffs=[{0: [True, True], 1: [True, True]}])
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
    ]

    observable = _check_compaction_distinguishable(
        core,
        ctx,
        scan_pattern=object(),
        generic_cell_map="generic_cell_map.json",
        unsupported="fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={"s1": 0},
        passed_fault_ids=[1],
    )

    assert observable == set()


@pytest.mark.unit
def test_check_compaction_distinguishable_missing_site_index_passes_through(
    _stub_kwargs: None,
) -> None:
    compaction_map = CompactionMap(1, [[0, 1]])
    ctx = _ctx(compaction_map)
    core = _FakeCore(lane_diffs=[])
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
    ]

    observable = _check_compaction_distinguishable(
        core,
        ctx,
        scan_pattern=object(),
        generic_cell_map="generic_cell_map.json",
        unsupported="fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={},  # s1 has no compiled index
        passed_fault_ids=[1],
    )

    assert observable == {1}
    # No fault was actually simulated -- the batch call still happens (with
    # an empty faults list) but nothing to look up.
    assert core.calls == [([], True)]


@pytest.mark.unit
def test_reject_compaction_indistinguishable_appends_rejection_and_block_for_every_fault() -> (
    None
):
    protocol_sim_rejections: list = []
    blocked: list = []

    _reject_compaction_indistinguishable(
        [1, 2, 3], "trackkey", protocol_sim_rejections, blocked
    )

    assert {r.fault_id for r in protocol_sim_rejections} == {1, 2, 3}
    assert all(
        r.reason_code == "compaction_indistinguishable" for r in protocol_sim_rejections
    )
    assert blocked == [(1, "trackkey"), (2, "trackkey"), (3, "trackkey")]


@pytest.mark.unit
def test_is_compaction_only_rejected_true_for_pure_compaction_history() -> None:
    assert _is_compaction_only_rejected({"compaction_indistinguishable"}) is True


@pytest.mark.unit
def test_is_compaction_only_rejected_false_for_mixed_history() -> None:
    # A mix must NOT be classified compaction-only -- it must keep falling
    # back to mark_fault_redundant, mirroring _is_compression_only_rejected's
    # regression-safety case.
    assert (
        _is_compaction_only_rejected(
            {"compaction_indistinguishable", "compression_unsatisfiable"}
        )
        is False
    )


@pytest.mark.unit
def test_is_compaction_only_rejected_false_for_no_history() -> None:
    assert _is_compaction_only_rejected(None) is False
    assert _is_compaction_only_rejected(set()) is False


@pytest.mark.unit
def test_undetected_reason_reports_compaction_unresolved() -> None:
    from faultflow.reporter.coverage import _undetected_reason

    assert (
        _undetected_reason(
            protocol_unresolved=False,
            compression_unresolved=False,
            compaction_unresolved=True,
            sat_outcome=None,
        )
        == "compaction_unresolved"
    )


@pytest.mark.unit
def test_undetected_reason_compression_takes_precedence_over_compaction() -> None:
    from faultflow.reporter.coverage import _undetected_reason

    # Both flagged: compression_unresolved wins (checked first in the chain),
    # matching how a fault's history is required to be single-reason before
    # either _is_*_only_rejected predicate ever fires in practice -- this
    # just pins the reporter's own precedence order directly.
    assert (
        _undetected_reason(
            protocol_unresolved=False,
            compression_unresolved=True,
            compaction_unresolved=True,
            sat_outcome=None,
        )
        == "compression_unresolved"
    )
