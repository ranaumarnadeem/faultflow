"""Unit tests for the compression check-and-retry hook in
faultflow.scan.detection_pipeline -- proves the rejection/blocked-pattern
wiring using a stubbed solve_xor_broadcast result, without needing a real
end-to-end SAT run (per the compression plan's TDD increment 8)."""

from __future__ import annotations

import pytest

import faultflow.scan.detection_pipeline as dp
from faultflow.scan.compression import CompressionMap
from faultflow.scan.detection_pipeline import (
    ScanPipelineContext,
    _FaultRow,
    _check_compression_satisfiable,
    _reject_compression_unsatisfiable,
)
from faultflow.scan.ring_generator import care_bit_rows, lookup_polynomial


def _ctx(compression_map, rows) -> ScanPipelineContext:
    return ScanPipelineContext(
        cfg=None,  # not read by _check_compression_satisfiable itself
        manifest={"max_chain_length": 2},
        generic_json="generic.json",
        pseudo_port_map={},
        functional_output_order=[],
        q_stem_site_keys=frozenset(),
        compression_map=compression_map,
        compression_care_bit_rows=rows,
    )


class _FakeCore:
    """Stubs simulate_scan_protocol_faults (every fault 'passes', i.e. every
    trial load_seqs still detects every requested fault -- makes every
    position look like a don't-care, so extraction returns an empty cube) and
    solve_xor_broadcast (result forced by the test)."""

    def __init__(self, solve_ok: bool) -> None:
        self.solve_ok = solve_ok
        self.solve_calls: list[tuple[int, list, list]] = []

    def simulate_scan_protocol_faults(self, *args, faults, **kwargs):
        batches = [
            {
                "batch_index": 0,
                "lanes": [
                    {"fault_index": i, "outcome": "pass"} for i in range(len(faults))
                ],
            }
        ]
        return {"golden_real_po_values": {}, "golden_unload_seqs": {}, "batches": batches}

    def solve_xor_broadcast(self, num_channels, fanout, care_bits):
        self.solve_calls.append((num_channels, fanout, care_bits))
        return {"ok": self.solve_ok, "channels": [False] * num_channels}


@pytest.fixture
def _stub_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dp,
        "_protocol_fault_sim_kwargs",
        lambda ctx, pattern: {
            "clock_ports": ["clk"],
            "scan_enable_port": "scan_en",
            "scan_input_ports": ["scan_in_0"],
            "scan_output_ports": ["scan_out_0"],
            "functional_output_ports": [],
            "max_chain_length": 2,
            "load_seqs": pattern.load_seqs,
            "capture_pi_values": {},
        },
    )


@pytest.mark.unit
def test_check_compression_satisfiable_true_when_solver_says_ok(
    _stub_kwargs: None,
) -> None:
    polynomial = lookup_polynomial(8)
    phase_shifter_taps = [[0], [0, 1]]
    compression_map = CompressionMap(8, polynomial, phase_shifter_taps)
    rows = care_bit_rows(polynomial, phase_shifter_taps, 2)
    ctx = _ctx(compression_map, rows)
    core = _FakeCore(solve_ok=True)

    scan_pattern = type(
        "P", (), {"load_seqs": {0: [False, True], 1: [True, False]}}
    )()
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
    ]

    ok = _check_compression_satisfiable(
        core,
        ctx,
        scan_pattern,
        "generic_cell_map.json",
        "fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={"s1": 0},
        passed_fault_ids=[1],
    )

    assert ok is True
    assert core.solve_calls  # solver was actually consulted


@pytest.mark.unit
def test_check_compression_satisfiable_false_when_solver_says_unsatisfiable(
    _stub_kwargs: None,
) -> None:
    polynomial = lookup_polynomial(8)
    phase_shifter_taps = [[0], [0, 1]]
    compression_map = CompressionMap(8, polynomial, phase_shifter_taps)
    rows = care_bit_rows(polynomial, phase_shifter_taps, 2)
    ctx = _ctx(compression_map, rows)
    core = _FakeCore(solve_ok=False)

    scan_pattern = type(
        "P", (), {"load_seqs": {0: [False, True], 1: [True, False]}}
    )()
    active_rows = [
        _FaultRow(fault_id=1, fault_site_key="s1", fault_type="sa0", net_index=0),
    ]

    ok = _check_compression_satisfiable(
        core,
        ctx,
        scan_pattern,
        "generic_cell_map.json",
        "fail",
        is_loc=False,
        is_los=False,
        head_bits={},
        active_clock_ports=None,
        active_rows=active_rows,
        generic_site_index={"s1": 0},
        passed_fault_ids=[1],
    )

    assert ok is False


@pytest.mark.unit
def test_reject_compression_unsatisfiable_appends_rejection_and_block_for_every_fault() -> (
    None
):
    protocol_sim_rejections: list = []
    blocked: list = []

    _reject_compression_unsatisfiable(
        [1, 2, 3], "trackkey", protocol_sim_rejections, blocked
    )

    assert {r.fault_id for r in protocol_sim_rejections} == {1, 2, 3}
    assert all(
        r.reason_code == "compression_unsatisfiable" for r in protocol_sim_rejections
    )
    assert blocked == [(1, "trackkey"), (2, "trackkey"), (3, "trackkey")]
