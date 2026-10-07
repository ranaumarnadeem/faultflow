"""A composed chip's scan chains, traced on its cells (faultflow.project.chip)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.chip import chip_manifest, trace_chains
from faultflow.project.manifest import ProjectError

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads(
    (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
)


def _scan(clk: int, d: int, sdi: int, se: int, q: int, *, wrapper: bool = False) -> Any:
    return {
        "type": "$scanff_faultflow",
        "attributes": {"faultflow_wrapper_role": "ff"} if wrapper else {},
        "port_directions": {
            "CLK": "input",
            "D": "input",
            "SDI": "input",
            "SE": "input",
            "Q": "output",
        },
        "connections": {"CLK": [clk], "D": [d], "SDI": [sdi], "SE": [se], "Q": [q]},
    }


def _buf(a: int, x: int) -> Any:
    return {
        "type": "sky130_fd_sc_hd__buf_1",
        "port_directions": {"A": "input", "X": "output"},
        "connections": {"A": [a], "X": [x]},
    }


def _chip() -> dict[str, Any]:
    """clk 2, scan_en 3; si0 4 -> u_a__r0 -> buffer -> u_b__r0 -> so0; si1 5 ->
    u_a__w0 (a wrapper flop) -> so1."""
    cells = {
        "u_a__r0": _scan(2, 20, 4, 3, 10),
        "g_buf": _buf(10, 11),
        "u_b__r0": _scan(2, 21, 11, 3, 12),
        "u_a__w0": _scan(2, 22, 5, 3, 13, wrapper=True),
    }
    ports = {
        "clk": {"direction": "input", "bits": [2]},
        "scan_en": {"direction": "input", "bits": [3]},
        "si": {"direction": "input", "bits": [4, 5]},
        "so": {"direction": "output", "bits": [12, 13]},
    }
    return {"ports": ports, "cells": cells, "netnames": {}}


def test_each_chain_runs_from_a_chip_input_through_buffers_to_an_output() -> None:
    chains = trace_chains(_chip(), CELL_MAP)
    assert [(c.scan_in, c.scan_out, c.cells, c.kind) for c in chains] == [
        ("si[0]", "so[0]", ("u_a__r0", "u_b__r0"), "core"),
        ("si[1]", "so[1]", ("u_a__w0",), "wrapper"),
    ]


def test_the_manifest_is_scans_shape(tmp_path: Path) -> None:
    path = tmp_path / "chip.json"
    module = _chip()
    path.write_text(json.dumps({"modules": {"soc": module}}), encoding="utf-8")
    manifest = chip_manifest(
        module, "soc", CELL_MAP, path, original_types={"u_a__r0": "dfxtp_1"}
    )
    assert (manifest["scan_inputs"], manifest["scan_outputs"]) == (
        ["si[0]", "si[1]"],
        ["so[0]", "so[1]"],
    )
    assert manifest["scan_enable"] == "scan_en"
    assert manifest["clock_nets"] == [2]
    assert [c["kind"] for c in manifest["chains"]] == ["core", "wrapper"]
    assert (manifest["max_chain_length"], manifest["min_chain_length"]) == (2, 1)
    first, second, wrapper = manifest["cells"]
    assert (first["instance"], first["original_type"], first["q_net"]) == (
        "u_a__r0",
        "dfxtp_1",
        10,
    )
    assert (second["chain_position"], second["scan_in_net"]) == (1, 11)
    assert (wrapper["chain_index"], wrapper["original_type"]) == (
        1,
        "$scanff_faultflow",
    )


def test_a_mixed_chain_or_a_stray_scan_cell_is_refused() -> None:
    mixed = _chip()
    mixed["cells"]["u_b__r0"]["attributes"] = {"faultflow_wrapper_role": "ff"}
    with pytest.raises(ProjectError, match="both wrapper and core"):
        trace_chains(mixed, CELL_MAP)
    stray = _chip()
    stray["cells"]["u_c__r0"] = _scan(2, 23, 30, 3, 31)
    with pytest.raises(ProjectError, match="on no chain"):
        trace_chains(stray, CELL_MAP)
    forked = _chip()
    forked["cells"]["u_c__r0"] = _scan(2, 23, 4, 3, 31)
    with pytest.raises(ProjectError, match="loads several scan cells"):
        trace_chains(forked, CELL_MAP)
    two_enables = _chip()
    two_enables["cells"]["u_b__r0"]["connections"]["SE"] = [2]
    with pytest.raises(ProjectError, match="scan enable"):
        chip_manifest(two_enables, "soc", CELL_MAP, Path("unused.json"))
