from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_scan_chain.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.golden
def test_simulate_scan_protocol_faults_matches_golden_api(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    kwargs = {
        "clock_ports": ["CLK"],
        "scan_enable_port": "scan_en",
        "scan_input_ports": ["scan_in"],
        "scan_output_ports": ["scan_out_0"],
        "functional_output_ports": ["Q0", "Q1"],
        "max_chain_length": 3,
        "load_seqs": {0: [True, False, True]},
        "capture_pi_values": {"D0": True, "D1": False, "D2": True},
    }
    direct = core.simulate_scan_pattern(str(FIXTURE), str(CELL_MAP), **kwargs)
    protocol_sim = core.simulate_scan_protocol_faults(
        str(FIXTURE), str(CELL_MAP), faults=[], **kwargs
    )
    assert protocol_sim["golden_real_po_values"] == direct["real_po_values"]
    assert protocol_sim["golden_unload_seqs"] == direct["unload_seqs"]
    assert protocol_sim["batches"] == []


@pytest.mark.golden
def test_simulate_scan_protocol_faults_detects_active_capture_fault(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    site_rows = core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail")
    d0 = next(
        row["compiled_net_index"] for row in site_rows if row["yosys_net_id"] == 5
    )
    result = core.simulate_scan_protocol_faults(
        str(FIXTURE),
        str(CELL_MAP),
        clock_ports=["CLK"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in"],
        scan_output_ports=["scan_out_0"],
        functional_output_ports=["Q0", "Q1"],
        max_chain_length=3,
        load_seqs={0: [True, False, True]},
        capture_pi_values={"D0": True, "D1": False, "D2": True},
        faults=[(d0, 0)],
    )
    assert result["batches"][0]["lanes"][0]["outcome"] == "pass"


@pytest.mark.golden
def test_simulate_scan_protocol_faults_sim_threads_is_deterministic(
    require_cpp_core: None,
) -> None:
    """Grading across the independent fault batches must be bit-identical through
    the binding (GIL released around the C++ grade) for any thread count."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    site_rows = core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail")
    nets = [row["compiled_net_index"] for row in site_rows]
    assert nets
    # 130 faults -> 3 batches, so multiple batches grade concurrently.
    faults = [(nets[i % len(nets)], i % 2) for i in range(130)]

    def run(sim_threads: int) -> dict:
        return core.simulate_scan_protocol_faults(
            str(FIXTURE),
            str(CELL_MAP),
            clock_ports=["CLK"],
            scan_enable_port="scan_en",
            scan_input_ports=["scan_in"],
            scan_output_ports=["scan_out_0"],
            functional_output_ports=["Q0", "Q1"],
            max_chain_length=3,
            load_seqs={0: [True, False, True]},
            capture_pi_values={"D0": True, "D1": False, "D2": True},
            faults=faults,
            sim_threads=sim_threads,
        )

    serial = run(1)
    assert len(serial["batches"]) == 3
    for threads in (2, 4, 8):
        assert run(threads) == serial
