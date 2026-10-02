"""End-to-end wiring test (compression plan TDD increment 10): insert
compression -> build the manifest a real writer would produce -> structural
check -> a real GF(2) seed solve for a genuine fault's care bit, and the
decompressor's whole load for that seed detecting the fault in the real
protocol simulator, all against the actual Yosys-synthesized composed netlist
(no stubs) -- proving every piece built in increments 1-9 actually functions
together, not just in isolation.

This test proves the underlying MECHANISM (insertion, structural
verification, seed solving, the seed's load) is correct and composes
correctly, using a hand-assembled manifest dict -- it is not a `ff.py`
campaign smoke test. The real, production writer of this manifest shape is
`Runner.scan_compress()` (`faultflow/runner/runner.py`, the `ff.py
scan-compress` CLI subcommand); see
`tests/python/scan/test_scan_compress_cli.py` for that real, CLI-driven
end-to-end coverage (real `ff.py scan` -> `scan-check` -> `scan-compress` ->
`rule_check`), and test_compressed_atpg_flow.py for ATPG through it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.compression import insert_compression
from faultflow.scan.compression_checks import check_compression_structure
from faultflow.scan.ring_generator import bitmask_to_index_list, care_bit_rows


def _scan_ff_cell(clk: int, d: int, sdi: int, se: int, q: int) -> dict:
    return {
        "hide_name": 0,
        "type": "$scanff_faultflow",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            "CLK": "input",
            "D": "input",
            "SDI": "input",
            "SE": "input",
            "Q": "output",
        },
        "connections": {"CLK": [clk], "D": [d], "SDI": [sdi], "SE": [se], "Q": [q]},
    }


def _real_two_chain_core_fixture() -> dict:
    return {
        "creator": "compression end-to-end fixture",
        "modules": {
            "core_top": {
                "attributes": {"top": 1},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "Q": {"direction": "output", "bits": [5]},
                    "scan_in_0": {"direction": "input", "bits": [6]},
                    "scan_in_1": {"direction": "input", "bits": [10]},
                    "scan_out_0": {"direction": "output", "bits": [5]},
                    "scan_out_1": {"direction": "output", "bits": [11]},
                },
                "cells": {
                    "u0": _scan_ff_cell(clk=2, d=3, sdi=6, se=4, q=5),
                    "u1": _scan_ff_cell(clk=2, d=3, sdi=10, se=4, q=11),
                },
                "netnames": {},
            }
        },
    }


@pytest.mark.integration
def test_compression_insertion_manifest_check_and_solve_end_to_end(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    # 1. Insert compression on a real scan-stitched core, via real Yosys
    #    synthesis (same mechanism proven in increments 3/5).
    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_two_chain_core_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    workdir = tmp_path / "work"
    output_json = tmp_path / "compressed.json"

    result_path, compression_map = insert_compression(
        core_json_path,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        num_channels=8,
        liberty=liberty,
        output_json=output_json,
        workdir=workdir,
        clock_port="CLK",
        scan_enable_port="scan_en",
    )
    assert result_path == output_json

    # 2. Build the manifest the real ff.py scan-compress writer produces
    #    (Runner.scan_compress()): "top" stays the PRE-compression design
    #    name (generic_json, omitted here since this test never calls
    #    check_scan_structure, would point at core_json_path) -- the composed
    #    netlist's own path/top name live under compression.composed_json/
    #    composed_top, never under the top-level generic_json key.
    manifest = {
        "top": "core_top",
        "compression": {
            "enabled": True,
            "num_channels": compression_map.num_channels,
            "tap_source_net": "effective_state",
            "scan_in_ports": ["scan_in_0", "scan_in_1"],
            "phase_shifter_taps": compression_map.phase_shifter_taps,
            "composed_json": str(output_json),
            "composed_top": "core_top_compressed",
            "scan_enable_port": "scan_en",
        },
    }

    # 3. Structural check (increment 9) must pass against the real netlist.
    structural = check_compression_structure(manifest)
    assert structural.passed, structural.errors

    # 4. A seed's whole load -- care_bit_rows for every chain at every shift
    #    cycle -- driven into the ORIGINAL, uncompressed netlist through the
    #    real protocol simulator, exactly as the scan pipeline grades a
    #    compressed pattern (detection_pipeline._decompressed): the
    #    decompressor doesn't exist in the netlist ATPG grades, only its rows
    #    do. u0 is chain 0's only flop, so it holds the stream's last bit, and
    #    the Q output shows it at capture. The GF(2) solve recovers, from any
    #    seed's whole load, a seed making that same load. (An earlier version
    #    extracted care bits for "u1's D stuck-at-0", passing D's Yosys net id
    #    where the simulator takes a compiled index: it graded another net,
    #    which its load never detected, so every position came out "care".)
    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    max_chain_length = 4

    def loaded_q(load_seqs: dict[int, list[bool]]) -> bool:
        result = dict(
            core.simulate_scan_protocol_faults(
                json_path=str(core_json_path),
                cell_map_path=cell_map,
                clock_ports=["CLK"],
                clock_off_states=[False],
                scan_enable_port="scan_en",
                scan_input_ports=["scan_in_0", "scan_in_1"],
                scan_output_ports=["scan_out_0", "scan_out_1"],
                functional_output_ports=["Q"],
                max_chain_length=max_chain_length,
                load_seqs=load_seqs,
                capture_pi_values={},
                faults=[],
                active_clock_ports=["CLK"],
            )
        )
        return bool(result["golden_real_po_values"]["Q"])

    rows = care_bit_rows(
        compression_map.polynomial,
        compression_map.phase_shifter_taps,
        max_chain_length,
    )

    def load_of(seed: int) -> dict[int, list[bool]]:
        return {
            chain: [
                bin(rows[cycle][chain] & seed).count("1") % 2 == 1
                for cycle in range(max_chain_length)
            ]
            for chain in (0, 1)
        }

    positions = [
        (chain, cycle) for chain in (0, 1) for cycle in range(max_chain_length)
    ]
    fanout = [bitmask_to_index_list(rows[cycle][chain]) for chain, cycle in positions]
    loaded = set()
    for seed in range(256):
        load = load_of(seed)
        if seed % 15 == 0:
            loaded.add(loaded_q(load))
            assert loaded_q(load) == load[0][max_chain_length - 1], seed
        solved = core.solve_xor_broadcast(
            compression_map.num_channels,
            fanout,
            [(i, load[chain][cycle]) for i, (chain, cycle) in enumerate(positions)],
        )
        assert solved["ok"] is True
        recovered = sum(1 << k for k, bit in enumerate(solved["channels"]) if bit)
        assert load_of(recovered) == load
    assert loaded == {False, True}
