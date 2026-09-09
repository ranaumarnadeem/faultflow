"""Golden equivalence test: the actual synthesized ring-generator RTL must
produce EXACTLY the same per-cycle scan-chain trajectory that
faultflow.scan.ring_generator.care_bit_rows predicts, for a real seed, driven
through the real C++ simulation engine (not a second, hand-rolled model that
could silently drift from what's actually simulated). This is the anti-drift
gate for the single-source-of-truth design in ring_generator.py -- see the
compression plan's "golden equivalence test" section.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.compression import insert_compression
from faultflow.scan.ring_generator import care_bit_rows


def _scan_ff_cell(clk: int, d: int, sdi: int, se: int, q: int) -> dict:
    """A synthetic $scanff_faultflow cell -- see test_compression.py's
    identically-named helper for the full rationale; duplicated here (not
    imported) to keep this test module self-contained."""
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
    """See test_compression.py's identically-named helper for the net-id map
    documentation -- duplicated here to keep this test module self-contained."""
    return {
        "creator": "compression test fixture",
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
def test_ring_generator_rtl_matches_python_model_across_full_cycle(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_two_chain_core_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    workdir = tmp_path / "work"
    output_json = tmp_path / "compressed.json"

    _, compression_map = insert_compression(
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

    max_chain_length = 16
    seed = 0b10110010  # arbitrary fixed 8-bit seed
    capture_pi_values = {f"tdi[{k}]": bool((seed >> k) & 1) for k in range(8)}

    predicted_rows = care_bit_rows(
        compression_map.polynomial, compression_map.phase_shifter_taps,
        max_chain_length,
    )

    def predicted_bit(cycle: int, chain: int) -> bool:
        row = predicted_rows[cycle][chain]
        return bin(row & seed).count("1") % 2 == 1

    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    for chain_id, scan_out in enumerate(["scan_out_0", "scan_out_1"]):
        result = core.simulate_scan_pattern(
            json_path=str(output_json),
            cell_map_path=cell_map,
            clock_ports=["CLK"],
            clock_off_states=[False],
            scan_enable_port="scan_en",
            # "D" is a dummy chain-0 scan-input placeholder -- the compressed
            # netlist has no real per-chain scan_in port anymore (they're
            # internal wires now, driven by the ring generator), but
            # simulate_scan_pattern requires scan_input_ports and
            # scan_output_ports to be the same length. D is a harmless real
            # input port we don't care about the value of here.
            scan_input_ports=["D"],
            scan_output_ports=[scan_out],
            functional_output_ports=[],
            max_chain_length=max_chain_length,
            load_seqs={},
            capture_pi_values=capture_pi_values,
            active_clock_ports=["CLK"],
        )
        observed = result["unload_seqs"][0]
        assert len(observed) == max_chain_length
        # Reseed fires (combinationally, before the first unload clock edge)
        # because scan_en rises 0->1 coming out of the single capture cycle,
        # restarting the ring generator from the SAME seed (tdi is held
        # constant via capture_pi_values across load/capture/unload) -- this
        # is exactly the "harmless second reseed" property documented in
        # ring_generator_wrapper_verilog's docstring, exercised for real here.
        #
        # unload_seqs[chain][k] samples the scan chain's OWN registered Q
        # output before offset k's edge (append_unload_pulse samples at the
        # clock-low sub-cycle, then toggles the clock high) -- so index 0
        # reflects whatever the scan FF held over from the single capture
        # cycle (not the ring generator at all -- there is no principled
        # prediction for it), and index k (k>=1) reflects what SDI/scan_in_N
        # was during offset (k-1)'s edge, i.e. care_bit_rows[k-1] (this is
        # the scan FF's own one-cycle shift-register delay, not a
        # ring-generator artifact -- confirmed empirically: it holds
        # regardless of seed, and disappears for k>=1 across two different
        # seeds and three different max_chain_length values).
        for k in range(1, max_chain_length):
            assert observed[k] == predicted_bit(k - 1, chain_id), (
                f"chain {chain_id} cycle {k}: RTL simulation observed "
                f"{observed[k]}, but the Python ring-generator model "
                f"predicted {predicted_bit(k - 1, chain_id)} -- the "
                "synthesized hardware and care_bit_rows have drifted out "
                "of sync"
            )
