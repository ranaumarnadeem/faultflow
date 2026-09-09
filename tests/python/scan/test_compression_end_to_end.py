"""End-to-end wiring test (compression plan TDD increment 10): insert
compression -> build the manifest a real writer would produce -> structural
check -> real care-bit extraction + real GF(2) solve against a genuine fault,
all against the actual Yosys-synthesized, CaDiCaL/GoldenRefSim-simulated
composed netlist (no stubs) -- proving every piece built in increments 1-9
actually functions together, not just in isolation.

This test proves the underlying MECHANISM (insertion, structural
verification, care-bit extraction, solving) is correct and composes
correctly, using a hand-assembled manifest dict -- it is not a `ff.py`
campaign smoke test. The real, production writer of this manifest shape is
`Runner.scan_compress()` (`faultflow/runner/runner.py`, the `ff.py
scan-compress` CLI subcommand); see
`tests/python/scan/test_scan_compress_cli.py` for that real, CLI-driven
end-to-end coverage (real `ff.py scan` -> `scan-check` -> `scan-compress` ->
`rule_check`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.care_bits import extract_scan_care_bits
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
        },
    }

    # 3. Structural check (increment 9) must pass against the real netlist.
    structural = check_compression_structure(manifest)
    assert structural.passed, structural.errors

    # 4. Real care-bit extraction + real GF(2) solve (increments 2/6),
    #    against a genuine fault in the ORIGINAL, uncompressed netlist --
    #    exactly as the real architecture works (and as
    #    _check_compression_satisfiable, increment 8, actually calls it):
    #    ATPG reasons about and extracts care bits from the design's own
    #    scan_in_0/scan_in_1 ports (the compressor doesn't exist from ATPG's
    #    point of view), and the compressor-achievability question is a
    #    SEPARATE, pure-math solve against care_bit_rows -- it never requires
    #    simulating the compressed netlist itself. core.simulate_scan_protocol_
    #    faults here is the real (not stubbed) detect oracle.
    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    core_module = json.loads(core_json_path.read_text(encoding="utf-8"))["modules"][
        "core_top"
    ]
    # A real fault site: scan FF u1's D input net (SA0).
    d_net = core_module["cells"]["u1"]["connections"]["D"][0]

    max_chain_length = 4
    # A load_seqs that genuinely detects the fault: shifting a 1 into chain 1
    # at cycle 0 (captured into u1's Q, then observed via unload) detects
    # u1's D stuck-at-0 (u1's D is tied to the shared top-level D port, so
    # this specific fixture's fault model is simplistic, but the MECHANISM
    # under test -- extraction + solve against a real oracle -- is what
    # matters here, not a rich fault set).
    load_seqs = {0: [False] * max_chain_length, 1: [True, False, False, False]}

    def detect(trial_load_seqs, targets):
        result = dict(
            core.simulate_scan_protocol_faults(
                json_path=str(core_json_path),
                cell_map_path=cell_map,
                clock_ports=["CLK"],
                clock_off_states=[False],
                scan_enable_port="scan_en",
                scan_input_ports=["scan_in_0", "scan_in_1"],
                scan_output_ports=["scan_out_0", "scan_out_1"],
                functional_output_ports=[],
                max_chain_length=max_chain_length,
                load_seqs=trial_load_seqs,
                capture_pi_values={},
                faults=[(d_net, 0)],
                active_clock_ports=["CLK"],
            )
        )
        passed = set()
        for batch in result.get("batches", []):
            for lane in batch.get("lanes", []):
                if str(dict(lane).get("outcome")) == "pass":
                    passed.add(1)
        return passed & targets

    care = extract_scan_care_bits(detect, load_seqs, max_chain_length, {1})
    assert care, "extraction found no care bits at all -- oracle wiring is broken"

    rows = care_bit_rows(
        compression_map.polynomial, compression_map.phase_shifter_taps,
        max_chain_length,
    )
    fanout = [bitmask_to_index_list(rows[cycle][chain]) for chain, cycle, _ in care]
    care_bits = [(i, value) for i, (_, _, value) in enumerate(care)]
    solved = core.solve_xor_broadcast(
        compression_map.num_channels, fanout, care_bits
    )
    # 8 channels vs. this tiny 2-chain/4-cycle design's handful of care bits
    # is comfortably within the compressor's capacity -- expected satisfiable.
    assert solved["ok"] is True
