from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.compression import (
    build_broadcast_fanout,
    insert_compression,
    ring_generator_wrapper_verilog,
)
from faultflow.scan.ring_generator import LfsrPolynomial, lookup_polynomial


def _gf2_rank(rows: list[list[int]], num_cols: int) -> int:
    """Rank over GF(2) via plain Gauss-Jordan elimination on bit-vector rows.

    Self-contained (no dependency on the C++ solve_xor_broadcast binding).
    Each row is a list of set-bit column indices; rows are converted to Python
    ints used as bitmasks for cheap XOR-based elimination.
    """
    masks = [sum(1 << c for c in row) for row in rows]
    rank = 0
    for col in range(num_cols):
        pivot = None
        for i in range(rank, len(masks)):
            if masks[i] & (1 << col):
                pivot = i
                break
        if pivot is None:
            continue
        masks[rank], masks[pivot] = masks[pivot], masks[rank]
        for i in range(len(masks)):
            if i != rank and masks[i] & (1 << col):
                masks[i] ^= masks[rank]
        rank += 1
    return rank


@pytest.mark.unit
@pytest.mark.parametrize(
    "num_channels,num_internal_bits", [(1, 1), (3, 3), (4, 10), (7, 56)]
)
def test_build_broadcast_fanout_covers_every_channel(
    num_channels: int, num_internal_bits: int
) -> None:
    fanout = build_broadcast_fanout(num_channels, num_internal_bits)
    seen = {channel for row in fanout for channel in row}
    assert seen == set(range(num_channels)), (
        f"channel(s) {set(range(num_channels)) - seen} never appear in any "
        "internal bit's fanout -- structurally dead channel"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "num_channels,num_internal_bits", [(1, 1), (3, 3), (4, 10), (7, 56)]
)
def test_build_broadcast_fanout_is_full_column_rank(
    num_channels: int, num_internal_bits: int
) -> None:
    """The fanout-to-internal-bit matrix must have full column rank (==
    num_channels) over GF(2) -- otherwise two or more phase-shifter inputs are
    indistinguishable from every internal bit's point of view (a real,
    structural degeneracy)."""
    fanout = build_broadcast_fanout(num_channels, num_internal_bits)
    rank = _gf2_rank(fanout, num_channels)
    assert rank == num_channels


@pytest.mark.unit
def test_build_broadcast_fanout_entries_are_sorted_unique_valid_indices() -> None:
    fanout = build_broadcast_fanout(4, 20)
    assert len(fanout) == 20
    for row in fanout:
        assert row == sorted(set(row)), "duplicate/unsorted channel index in one row"
        assert all(0 <= c < 4 for c in row)
        assert len(row) >= 1, "an internal bit with an empty fanout is unreachable"


@pytest.mark.unit
def test_build_broadcast_fanout_rejects_nonpositive_args() -> None:
    with pytest.raises(ValueError, match="num_channels"):
        build_broadcast_fanout(0, 10)
    with pytest.raises(ValueError, match="num_internal_bits"):
        build_broadcast_fanout(4, 0)


def _tiny_two_chain_core_json() -> dict:
    """A minimal hand-built Yosys-JSON fixture: one module, 2 scan chains (so
    2 separate scan_in_N ports, matching stitch.py's scan_port_names()
    convention -- not one N-bit bus), plus a clock and scan-enable port that
    double as the ring generator's clock/reseed-trigger. No cells needed --
    ring_generator_wrapper_verilog only reads the ports dict."""
    return {
        "modules": {
            "core_top": {
                "attributes": {"top": 1},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "scan_en": {"direction": "input", "bits": [3]},
                    "data_out": {"direction": "output", "bits": [4, 5, 6, 7]},
                    "scan_in_0": {"direction": "input", "bits": [8]},
                    "scan_in_1": {"direction": "input", "bits": [9]},
                    "scan_out_0": {"direction": "output", "bits": [10]},
                    "scan_out_1": {"direction": "output", "bits": [11]},
                },
                "cells": {},
                "netnames": {},
            }
        }
    }


_TWO_BIT_POLY = LfsrPolynomial(2, frozenset({1}))


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_passes_through_non_scan_in_ports() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = ring_generator_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        _TWO_BIT_POLY,
        fanout,
        "core_top_compressed",
    )
    assert "input clk;" in rtl
    assert "input scan_en;" in rtl
    assert "output [3:0] data_out;" in rtl
    assert "output scan_out_0;" in rtl
    assert "output scan_out_1;" in rtl
    assert "input scan_in_0;" not in rtl
    assert "input scan_in_1;" not in rtl


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_declares_channel_bus_and_instantiates_core() -> (
    None
):
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = ring_generator_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        _TWO_BIT_POLY,
        fanout,
        "core_top_compressed",
    )
    assert "input [1:0] tdi;" in rtl
    assert "module core_top_compressed" in rtl
    assert "core_top core_inst" in rtl
    assert ".clk(clk)" in rtl
    assert ".data_out(data_out)" in rtl
    assert ".scan_out_0(scan_out_0)" in rtl


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_tap_mask_matches_polynomial() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = ring_generator_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        _TWO_BIT_POLY,
        fanout,
        "core_top_compressed",
    )
    assert "TAP_MASK = 2'b10;" in rtl


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_phase_shifter_assigns_match_taps() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = ring_generator_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        _TWO_BIT_POLY,
        fanout,
        "core_top_compressed",
    )
    assert "assign scan_in_0 = effective_state[0];" in rtl
    assert "assign scan_in_1 = effective_state[0] ^ effective_state[1];" in rtl
    assert ".scan_in_0(scan_in_0)" in rtl
    assert ".scan_in_1(scan_in_1)" in rtl


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_reseed_and_clock_present() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = ring_generator_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_in_0", "scan_in_1"],
        _TWO_BIT_POLY,
        fanout,
        "core_top_compressed",
    )
    assert "reseed = scan_en & ~prev_scan_en" in rtl
    assert "always @(posedge clk)" in rtl
    assert "reg [1:0] lfsr_reg" in rtl
    assert "reg prev_scan_en" in rtl


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_rejects_fanout_length_mismatch() -> None:
    core_json = _tiny_two_chain_core_json()
    with pytest.raises(ValueError, match="fanout"):
        ring_generator_wrapper_verilog(
            core_json,
            "core_top",
            ["scan_in_0", "scan_in_1"],
            _TWO_BIT_POLY,
            [[0]],
            "wrap",
        )


@pytest.mark.unit
def test_ring_generator_wrapper_verilog_rejects_missing_scan_enable_or_clock_port() -> (
    None
):
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    with pytest.raises(ValueError, match="scan_enable_port"):
        ring_generator_wrapper_verilog(
            core_json,
            "core_top",
            ["scan_in_0", "scan_in_1"],
            _TWO_BIT_POLY,
            fanout,
            "wrap",
            scan_enable_port="no_such_port",
        )
    with pytest.raises(ValueError, match="clock_port"):
        ring_generator_wrapper_verilog(
            core_json,
            "core_top",
            ["scan_in_0", "scan_in_1"],
            _TWO_BIT_POLY,
            fanout,
            "wrap",
            clock_port="no_such_clock",
        )


def _scan_ff_cell(clk: int, d: int, sdi: int, se: int, q: int) -> dict:
    """A synthetic $scanff_faultflow cell, same shape stitch.py itself writes
    (and the same convention tests/python/soc2_fixtures.py uses) -- Yosys
    never sees this cell type (it's grafted in post-synthesis by compose_soc,
    never re-synthesized), so it doesn't need to be a real Liberty cell."""
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
    """A real (not just a ports-dict stub), splice-able core netlist: 2
    independent 1-FF scan chains via synthetic $scanff_faultflow cells, same
    net-id-map documentation style as soc2_fixtures.py.

    2=CLK 3=D 4=scan_en 5=Q(=scan_out_0, same net -- a 1-FF chain's serial
    output IS its own Q) 6=scan_in_0 10=scan_in_1 11=scan_out_1
    """
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


@pytest.mark.unit
def test_insert_compression_produces_composed_netlist(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

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
    assert compression_map.num_channels == 8
    assert compression_map.polynomial == lookup_polynomial(8)
    assert len(compression_map.phase_shifter_taps) == 2

    composed = json.loads(output_json.read_text(encoding="utf-8"))
    module = composed["modules"]["core_top_compressed"]
    # The frozen core's own 2 scan-FF cells must have been spliced in verbatim
    # (name-prefixed "core__..."), proving compose_soc actually grafted them
    # in rather than the wrapper module standing alone.
    spliced_ff_names = [
        n for n in module["cells"] if n.endswith("u0") or n.endswith("u1")
    ]
    assert len(spliced_ff_names) == 2
    # The external channel bus must be a real top-level port on the composed
    # netlist (not just present in the intermediate wrapper RTL text).
    assert "tdi" in module["ports"]
    assert len(module["ports"]["tdi"]["bits"]) == 8
    # scan_in_0/scan_in_1 must NOT survive as external ports on the final
    # composed netlist -- they're internal wires now, driven by the ring
    # generator's phase shifter.
    assert "scan_in_0" not in module["ports"]
    assert "scan_in_1" not in module["ports"]
    # The ring generator's own register must have survived synthesis as real
    # sequential cells (not been optimized away), proving this is genuine
    # synthesizable hardware, not just wrapper text. Sky130 HD FF cell
    # families are named "*dfxtp_*"/"*edfxtp_*"/"*sdfxtp_*" etc. (see
    # CLAUDE.md's FF SEMANTICS section) -- "dfxtp" is the substring common to
    # all of them.
    ff_like_cells = [
        c for c in module["cells"].values() if "dfxtp" in c.get("type", "").lower()
    ]
    assert len(ff_like_cells) >= 8, (
        "expected at least 8 flip-flop cells for the ring generator's own "
        f"register, found {len(ff_like_cells)}"
    )
