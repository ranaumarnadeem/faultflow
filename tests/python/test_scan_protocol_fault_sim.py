from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_scan_chain.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.golden
def test_simulate_scan_protocol_faults_matches_golden_api(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    kwargs = {
        "clock_port": "CLK",
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
        clock_port="CLK",
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
