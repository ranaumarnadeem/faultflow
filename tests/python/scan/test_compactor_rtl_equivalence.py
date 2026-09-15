"""Golden equivalence test: the actual synthesized compactor RTL's `tdo`
output must equal the real, gate-simulated per-chain unload data XOR-folded
per CompactionMap.fanout -- driven through the real C++ simulation engine,
comparing two REAL simulations against each other rather than checking real
hardware against a hand-written Python model of scan-chain dynamics (unlike
test_ring_generator_rtl_equivalence.py's decompressor equivalence test, which
must trust care_bit_rows as a model of LFSR dynamics, this test only trusts
the XOR-fold bookkeeping itself -- CompactionMap.fanout -- since both sides
of the comparison are real simulated data). This is the behavioral
counterpart to compaction_checks.py's purely structural check_compaction_structure
(COMP002), which re-derives topology from netlist connectivity but never
toggles a gate.

Uses a 4-independent-D-input fixture (NOT test_compaction.py's
_real_four_chain_core_fixture, which ties all 4 chains to the SAME D net --
that aliasing would let a folding bug pass unnoticed). load_seqs is
deliberately {} and max_chain_length is deliberately 1: in this
one-FF-per-chain fixture topology, base_values() in scan_pattern_sim.cpp
forces every scan_input_ports entry to false on every cycle including
unload, so load_seqs never reaches any sampled position -- unload index 0 is
the only data-bearing sample (the FF's just-captured D value); anything
beyond it is trivial 0-vs-0 padding, not real coverage.

No FF-delay index-0 skip is needed here (unlike the ring-generator test):
that skip exists because the ring-generator test compares real RTL against
an independent shift-register MODEL with no principled prediction at index
0. Here both sides are real simulated quantities sampled at the identical
logical unload index across two calls with identical ScanPatternRequest
fields, and `tdo` is purely combinational (no register anywhere in the
compactor) -- the fold identity holds at every index, including 0.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.compaction import build_compactor_fanout, insert_compaction


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


def _real_four_independent_d_chain_fixture() -> dict:
    """4 independent 1-FF scan chains, each captured from its OWN D input --
    unlike test_compaction.py's _real_four_chain_core_fixture (all 4 chains
    share one D net), independent D inputs make the XOR-fold check
    non-trivial: a folding bug that always produces a constant would be
    masked by a shared-D fixture but not by this one.

    2=CLK 4=scan_en 6=scan_in_0 10=scan_out_0 11=scan_in_1 12=scan_out_1
    13=scan_in_2 14=scan_out_2 15=scan_in_3 16=scan_out_3
    20=D0 21=D1 22=D2 23=D3
    """
    return {
        "creator": "compactor RTL equivalence test fixture",
        "modules": {
            "core_top": {
                "attributes": {"top": 1},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "D0": {"direction": "input", "bits": [20]},
                    "D1": {"direction": "input", "bits": [21]},
                    "D2": {"direction": "input", "bits": [22]},
                    "D3": {"direction": "input", "bits": [23]},
                    "scan_in_0": {"direction": "input", "bits": [6]},
                    "scan_out_0": {"direction": "output", "bits": [10]},
                    "scan_in_1": {"direction": "input", "bits": [11]},
                    "scan_out_1": {"direction": "output", "bits": [12]},
                    "scan_in_2": {"direction": "input", "bits": [13]},
                    "scan_out_2": {"direction": "output", "bits": [14]},
                    "scan_in_3": {"direction": "input", "bits": [15]},
                    "scan_out_3": {"direction": "output", "bits": [16]},
                },
                "cells": {
                    "u0": _scan_ff_cell(clk=2, d=20, sdi=6, se=4, q=10),
                    "u1": _scan_ff_cell(clk=2, d=21, sdi=11, se=4, q=12),
                    "u2": _scan_ff_cell(clk=2, d=22, sdi=13, se=4, q=14),
                    "u3": _scan_ff_cell(clk=2, d=23, sdi=15, se=4, q=16),
                },
                "netnames": {},
            }
        },
    }


_SCAN_IN_PORTS = ["scan_in_0", "scan_in_1", "scan_in_2", "scan_in_3"]
_SCAN_OUT_PORTS = ["scan_out_0", "scan_out_1", "scan_out_2", "scan_out_3"]


def _real_chain_unload_bits(
    core, core_json_path: Path, cell_map: str, capture_pi_values: dict[str, bool]
) -> list[bool]:
    """Real gate-level simulation of the plain (uncompacted) core: returns
    each chain's single captured-and-unloaded bit (max_chain_length=1),
    indexed by chain id 0..3."""
    result = core.simulate_scan_pattern(
        json_path=str(core_json_path),
        cell_map_path=cell_map,
        clock_ports=["CLK"],
        clock_off_states=[False],
        scan_enable_port="scan_en",
        scan_input_ports=_SCAN_IN_PORTS,
        scan_output_ports=_SCAN_OUT_PORTS,
        functional_output_ports=[],
        max_chain_length=1,
        load_seqs={},
        capture_pi_values=capture_pi_values,
        active_clock_ports=["CLK"],
    )
    return [bool(result["unload_seqs"][c][0]) for c in range(4)]


@pytest.mark.integration
def test_compactor_rtl_single_output_matches_real_chain_xor_fold(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_four_independent_d_chain_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    output_json = tmp_path / "compacted.json"

    _, compaction_map = insert_compaction(
        core_json_path,
        "core_top",
        _SCAN_OUT_PORTS,
        num_outputs=1,
        liberty=liberty,
        output_json=output_json,
        workdir=tmp_path / "work",
    )
    assert compaction_map.fanout == build_compactor_fanout(1, 4)

    capture_pi_values = {"D0": True, "D1": False, "D2": True, "D3": True}
    chain_bits = _real_chain_unload_bits(
        core, core_json_path, cell_map, capture_pi_values
    )

    result = core.simulate_scan_pattern(
        json_path=str(output_json),
        cell_map_path=cell_map,
        clock_ports=["CLK"],
        clock_off_states=[False],
        scan_enable_port="scan_en",
        scan_input_ports=_SCAN_IN_PORTS,
        # "tdo" repeated 4x purely to satisfy the scan_input/output_ports
        # length-match constraint -- scan_input_ports and scan_output_ports
        # are independently resolved (see module docstring / plan), so this
        # is safe: every repetition reads the identical net.
        scan_output_ports=["tdo", "tdo", "tdo", "tdo"],
        functional_output_ports=[],
        max_chain_length=1,
        load_seqs={},
        capture_pi_values=capture_pi_values,
        active_clock_ports=["CLK"],
    )
    tdo_repeats = [result["unload_seqs"][i][0] for i in range(4)]
    assert tdo_repeats == [tdo_repeats[0]] * 4, "repeated 'tdo' name must alias"
    tdo_observed = bool(tdo_repeats[0])

    expected = False
    for chain_id in compaction_map.fanout[0]:
        expected ^= chain_bits[chain_id]
    assert tdo_observed == expected, (
        f"real synthesized tdo={tdo_observed} but XOR-fold of real chain "
        f"bits {chain_bits} over fanout[0]={compaction_map.fanout[0]} "
        f"predicts {expected} -- the compactor RTL and CompactionMap.fanout "
        "have drifted out of sync"
    )


@pytest.mark.integration
def test_compactor_rtl_multi_output_bracket_indexed_ports_alias_correctly(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Sanity check for a code path never exercised anywhere else in this
    codebase before this test: bracket-indexed OUTPUT port names
    (scan_output_ports=["tdo[0]", "tdo[1]", ...], as opposed to the
    established INPUT-side usage in test_ring_generator_rtl_equivalence.py's
    capture_pi_values). Must be proven safe before test 2b builds the full
    equivalence assertion on top of it."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_four_independent_d_chain_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    output_json = tmp_path / "compacted2.json"

    insert_compaction(
        core_json_path,
        "core_top",
        _SCAN_OUT_PORTS,
        num_outputs=2,
        liberty=liberty,
        output_json=output_json,
        workdir=tmp_path / "work2",
    )

    # build_compactor_fanout(2, 4): fanout[0]=[0,2,3], fanout[1]=[1,2] --
    # D0=0,D1=1,D2=0,D3=0 -> tdo[0]=XOR(0,0,0)=0, tdo[1]=XOR(1,0)=1: distinct,
    # proving tdo[0]/tdo[1] are genuinely different nets, not aliased.
    capture_pi_values = {"D0": False, "D1": True, "D2": False, "D3": False}

    result = core.simulate_scan_pattern(
        json_path=str(output_json),
        cell_map_path=cell_map,
        clock_ports=["CLK"],
        clock_off_states=[False],
        scan_enable_port="scan_en",
        scan_input_ports=_SCAN_IN_PORTS,
        scan_output_ports=["tdo[0]", "tdo[1]", "tdo[0]", "tdo[1]"],
        functional_output_ports=[],
        max_chain_length=1,
        load_seqs={},
        capture_pi_values=capture_pi_values,
        active_clock_ports=["CLK"],
    )

    bit0 = [result["unload_seqs"][0][0], result["unload_seqs"][2][0]]
    bit1 = [result["unload_seqs"][1][0], result["unload_seqs"][3][0]]
    assert bit0[0] == bit0[1], "repeated 'tdo[0]' must alias to the same bit"
    assert bit1[0] == bit1[1], "repeated 'tdo[1]' must alias to the same bit"
    assert bool(bit0[0]) != bool(bit1[0]), (
        "tdo[0] and tdo[1] must be genuinely distinct nets for this "
        "capture pattern, not aliased to each other"
    )
    assert bool(bit0[0]) is False
    assert bool(bit1[0]) is True


@pytest.mark.integration
def test_compactor_rtl_multi_output_matches_real_chain_xor_fold(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_four_independent_d_chain_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    cell_map = "cells/sky130/sky130_fd_sc_hd.json"
    output_json = tmp_path / "compacted3.json"

    _, compaction_map = insert_compaction(
        core_json_path,
        "core_top",
        _SCAN_OUT_PORTS,
        num_outputs=2,
        liberty=liberty,
        output_json=output_json,
        workdir=tmp_path / "work3",
    )
    assert compaction_map.fanout == build_compactor_fanout(2, 4)

    capture_pi_values = {"D0": True, "D1": False, "D2": True, "D3": True}
    chain_bits = _real_chain_unload_bits(
        core, core_json_path, cell_map, capture_pi_values
    )

    result = core.simulate_scan_pattern(
        json_path=str(output_json),
        cell_map_path=cell_map,
        clock_ports=["CLK"],
        clock_off_states=[False],
        scan_enable_port="scan_en",
        scan_input_ports=_SCAN_IN_PORTS,
        scan_output_ports=["tdo[0]", "tdo[1]", "tdo[0]", "tdo[1]"],
        functional_output_ports=[],
        max_chain_length=1,
        load_seqs={},
        capture_pi_values=capture_pi_values,
        active_clock_ports=["CLK"],
    )
    tdo_observed = [
        bool(result["unload_seqs"][0][0]),
        bool(result["unload_seqs"][1][0]),
    ]

    for o in range(2):
        expected = False
        for chain_id in compaction_map.fanout[o]:
            expected ^= chain_bits[chain_id]
        assert tdo_observed[o] == expected, (
            f"real synthesized tdo[{o}]={tdo_observed[o]} but XOR-fold of "
            f"real chain bits {chain_bits} over fanout[{o}]="
            f"{compaction_map.fanout[o]} predicts {expected} -- the "
            "compactor RTL and CompactionMap.fanout have drifted out of sync"
        )
