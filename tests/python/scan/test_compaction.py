from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.scan.compaction import (
    build_compactor_fanout,
    compactor_wrapper_verilog,
    insert_compaction,
)
from faultflow.scan.compaction_checks import check_compaction_structure


@pytest.mark.unit
def test_build_compactor_fanout_rejects_nonpositive_args() -> None:
    with pytest.raises(ValueError, match="num_outputs"):
        build_compactor_fanout(0, 10)
    with pytest.raises(ValueError, match="num_internal_bits"):
        build_compactor_fanout(4, 0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "num_outputs,num_internal_bits", [(1, 1), (2, 5), (3, 10), (4, 20)]
)
def test_build_compactor_fanout_every_chain_contributes(
    num_outputs: int, num_internal_bits: int
) -> None:
    """No chain has an all-zero column -- a lone differing chain always
    changes at least one compacted output bit (never silently invisible)."""
    fanout = build_compactor_fanout(num_outputs, num_internal_bits)
    assert len(fanout) == num_outputs
    contributes = {c for row in fanout for c in row}
    assert contributes == set(range(num_internal_bits))


@pytest.mark.unit
@pytest.mark.parametrize(
    "num_outputs,num_internal_bits", [(2, 2), (3, 4), (4, 8), (5, 16)]
)
def test_build_compactor_fanout_covers_every_output_bit(
    num_outputs: int, num_internal_bits: int
) -> None:
    """Once num_internal_bits >= 2**(num_outputs-1), every output bit is
    touched by at least one chain -- no structurally dead output wire."""
    fanout = build_compactor_fanout(num_outputs, num_internal_bits)
    for row in fanout:
        assert row, "an output bit with no contributing chain is dead hardware"


@pytest.mark.unit
@pytest.mark.parametrize("num_outputs", [2, 3, 4, 5])
def test_build_compactor_fanout_distinct_columns_within_one_cycle(
    num_outputs: int,
) -> None:
    """Within one full cycle (num_internal_bits <= 2**num_outputs - 1), every
    chain gets a DISTINCT column -- no two chains are structurally
    indistinguishable to the compactor."""
    period = (1 << num_outputs) - 1
    fanout = build_compactor_fanout(num_outputs, period)
    columns: dict[int, int] = {}
    for o, row in enumerate(fanout):
        for c in row:
            columns[c] = columns.get(c, 0) | (1 << o)
    assert len(set(columns.values())) == period


@pytest.mark.unit
def test_build_compactor_fanout_entries_sorted_and_valid() -> None:
    fanout = build_compactor_fanout(3, 10)
    for row in fanout:
        assert row == sorted(set(row))
        assert all(0 <= c < 10 for c in row)


def _tiny_two_chain_core_json() -> dict:
    """A minimal hand-built Yosys-JSON fixture: one module, 2 scan chains
    (scan_out_0/scan_out_1), plus a clock and one functional port.
    compactor_wrapper_verilog only reads the ports dict."""
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


@pytest.mark.unit
def test_compactor_wrapper_verilog_passes_through_non_scan_out_ports() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0, 1]]
    rtl = compactor_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_out_0", "scan_out_1"],
        fanout,
        "core_top_compacted",
    )
    assert "input clk;" in rtl
    assert "input scan_en;" in rtl
    assert "output [3:0] data_out;" in rtl
    assert "input scan_in_0;" in rtl
    assert "input scan_in_1;" in rtl
    assert "output scan_out_0;" not in rtl
    assert "output scan_out_1;" not in rtl


@pytest.mark.unit
def test_compactor_wrapper_verilog_declares_channel_bus_and_instantiates_core() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0, 1]]
    rtl = compactor_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_out_0", "scan_out_1"],
        fanout,
        "core_top_compacted",
    )
    assert "output tdo;" in rtl
    assert "module core_top_compacted" in rtl
    assert "core_top core_inst" in rtl
    assert ".clk(clk)" in rtl
    assert ".data_out(data_out)" in rtl
    assert ".scan_in_0(scan_in_0)" in rtl


@pytest.mark.unit
def test_compactor_wrapper_verilog_xor_assigns_match_fanout_map() -> None:
    core_json = _tiny_two_chain_core_json()
    fanout = [[0], [0, 1]]
    rtl = compactor_wrapper_verilog(
        core_json,
        "core_top",
        ["scan_out_0", "scan_out_1"],
        fanout,
        "core_top_compacted",
    )
    assert "assign tdo[0] = scan_out_0;" in rtl
    assert "assign tdo[1] = scan_out_0 ^ scan_out_1;" in rtl
    assert ".scan_out_0(scan_out_0)" in rtl
    assert ".scan_out_1(scan_out_1)" in rtl


@pytest.mark.unit
def test_compactor_wrapper_verilog_rejects_fanout_out_of_range_chain() -> None:
    core_json = _tiny_two_chain_core_json()
    with pytest.raises(ValueError, match="fanout"):
        compactor_wrapper_verilog(
            core_json,
            "core_top",
            ["scan_out_0", "scan_out_1"],
            [[0, 2]],
            "wrap",
        )


def _scan_ff_cell(clk: int, d: int, sdi: int, se: int, q: int) -> dict:
    """A synthetic $scanff_faultflow cell -- see test_compression.py's
    identically-named helper for the full rationale; duplicated here to keep
    this test module self-contained."""
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


def _real_four_chain_core_fixture() -> dict:
    """A real, splice-able core netlist: 4 independent 1-FF scan chains
    (enough chains to make a genuine 4->2 compaction meaningful), same
    net-id-map documentation style as test_compression.py's fixture.

    2=CLK 3=D 4=scan_en 5=Q 6=scan_in_0 10=scan_out_0
    11=scan_in_1 12=scan_out_1 13=scan_in_2 14=scan_out_2 15=scan_in_3 16=scan_out_3
    """
    return {
        "creator": "compaction test fixture",
        "modules": {
            "core_top": {
                "attributes": {"top": 1},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "Q": {"direction": "output", "bits": [5]},
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
                    "u0": _scan_ff_cell(clk=2, d=3, sdi=6, se=4, q=10),
                    "u1": _scan_ff_cell(clk=2, d=3, sdi=11, se=4, q=12),
                    "u2": _scan_ff_cell(clk=2, d=3, sdi=13, se=4, q=14),
                    "u3": _scan_ff_cell(clk=2, d=3, sdi=15, se=4, q=16),
                },
                "netnames": {},
            }
        },
    }


@pytest.mark.unit
def test_insert_compaction_produces_composed_netlist(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_four_chain_core_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    workdir = tmp_path / "work"
    output_json = tmp_path / "compacted.json"
    scan_out_ports = ["scan_out_0", "scan_out_1", "scan_out_2", "scan_out_3"]

    result_path, compaction_map = insert_compaction(
        core_json_path,
        "core_top",
        scan_out_ports,
        num_outputs=2,
        liberty=liberty,
        output_json=output_json,
        workdir=workdir,
    )

    assert result_path == output_json
    assert compaction_map.num_outputs == 2
    assert len(compaction_map.fanout) == 2

    composed = json.loads(output_json.read_text(encoding="utf-8"))
    module = composed["modules"]["core_top_compacted"]
    # The frozen core's own 4 scan-FF cells must have been spliced in
    # verbatim, proving compose_soc actually grafted them in.
    spliced_ff_names = [
        n for n in module["cells"] if any(n.endswith(f"u{i}") for i in range(4))
    ]
    assert len(spliced_ff_names) == 4
    # The external channel bus must be a real top-level port on the composed
    # netlist, 2 bits wide (the compacted channel count, not the 4 real chains).
    assert "tdo" in module["ports"]
    assert len(module["ports"]["tdo"]["bits"]) == 2
    # scan_out_0..3 must NOT survive as external ports -- they're internal
    # wires now, feeding the XOR compactor tree.
    for port in scan_out_ports:
        assert port not in module["ports"]
    # scan_in_0..3 are untouched, still real external ports (compaction is
    # output-side only).
    for i in range(4):
        assert f"scan_in_{i}" in module["ports"]


@pytest.mark.unit
def test_check_compaction_structure_passes_against_real_synthesized_netlist(
    tmp_path: Path,
) -> None:
    """End-to-end: insert_compaction's REAL Yosys/abc output must satisfy
    check_compaction_structure -- proves the hand-built fixtures in
    test_compaction_checks.py aren't masking a real-synthesis quirk (the
    exact class of gap compression_checks.py's module docstring warns
    about)."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    core_json_path = tmp_path / "core.json"
    core_json_path.write_text(
        json.dumps(_real_four_chain_core_fixture()), encoding="utf-8"
    )
    liberty = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
    workdir = tmp_path / "work"
    output_json = tmp_path / "compacted.json"
    scan_out_ports = ["scan_out_0", "scan_out_1", "scan_out_2", "scan_out_3"]

    _, compaction_map = insert_compaction(
        core_json_path,
        "core_top",
        scan_out_ports,
        num_outputs=2,
        liberty=liberty,
        output_json=output_json,
        workdir=workdir,
    )

    manifest = {
        "top": "core_top",
        "compaction": {
            "enabled": True,
            "num_outputs": compaction_map.num_outputs,
            "scan_out_ports": scan_out_ports,
            "channel_port": "tdo",
            "fanout": compaction_map.fanout,
            "composed_json": str(output_json),
            "composed_top": "core_top_compacted",
        },
    }

    result = check_compaction_structure(manifest)

    assert result.passed, result.errors
