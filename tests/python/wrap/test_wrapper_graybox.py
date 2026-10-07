"""A wrapped block's EXTEST graybox (faultflow.wrap.graybox): its boundary cells, the
core one blackbox, the wrapper chains alone; and its patterns as the whole block
takes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import stitch_scan_json
from faultflow.scan.reports import manifest_from_result
from faultflow.wrap.block import wrap_block
from faultflow.wrap.errors import WrapError
from faultflow.wrap.graybox import CORE, chip_patterns, extest_graybox
from faultflow.wrap.record import wrapper_record
from faultflow.wrap.sides import EXTEST, FAULT_TYPES, owner

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
TOP = "blk"


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}",
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in ("Q", "X") else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _block() -> dict[str, Any]:
    """clk 2, a 3, b 4; r0 = a & b, r1 = r0 ^ b, r2 = r1; y = r2 (via a buffer)."""
    cells = {
        "g0": _cell("and2_1", A=3, B=4, X=10),
        "r0": _cell("dfxtp_1", CLK=2, D=10, Q=11),
        "g1": _cell("xor2_1", A=11, B=4, X=12),
        "r1": _cell("dfxtp_1", CLK=2, D=12, Q=13),
        "r2": _cell("dfxtp_1", CLK=2, D=13, Q=14),
        "g2": _cell("buf_1", A=14, X=15),
    }
    ports = {
        "clk": {"direction": "input", "bits": [2]},
        "a": {"direction": "input", "bits": [3]},
        "b": {"direction": "input", "bits": [4]},
        "y": {"direction": "output", "bits": [15]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {"inner": {"hide_name": 0, "bits": [12], "attributes": {}}}
    return {"modules": {TOP: module}}


def _scanned(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """The block wrapped and scanned, its scan manifest with the wrapper record:
    the core's three flops on chain 0, the three boundary cells on wrapper chains
    1 and 2."""
    wrapped = tmp_path / "wrapped.json"
    netlist = wrap_block(_block(), TOP, CELL_MAP).netlist
    wrapped.write_text(json.dumps(netlist), encoding="utf-8")
    scanned = tmp_path / "scanned.json"
    result = stitch_scan_json(wrapped, CELL_MAP_PATH, TOP, scanned, wrapper_chains=2)
    manifest: dict[str, Any] = manifest_from_result(
        result, wrapped, tmp_path / "map.v", None
    )
    module = json.loads(scanned.read_text(encoding="utf-8"))["modules"][TOP]
    manifest["wrapper"] = wrapper_record(module, manifest["chains"])
    return scanned, manifest


def test_the_graybox_keeps_the_boundary_cells_and_makes_the_core_a_blackbox(
    tmp_path: Path,
) -> None:
    scanned, manifest = _scanned(tmp_path)
    netlist = json.loads(scanned.read_text(encoding="utf-8"))
    box = extest_graybox(netlist, manifest, CELL_MAP)
    module = box.netlist["modules"][TOP]
    wrapper = manifest["wrapper"]
    cells = {c[role] for c in wrapper["cells"] for role in ("mux", "gate", "ff")}
    assert set(module["cells"]) == cells | {CORE}
    assert module["ports"] == netlist["modules"][TOP]["ports"]
    stub = module["cells"][CORE]
    by_direction: dict[str, set[int]] = {"input": set(), "output": set()}
    for pin, bits in stub["connections"].items():
        by_direction[stub["port_directions"][pin]].update(bits)
    by_label = {c["label"]: c for c in wrapper["cells"]}
    # The core drives y's cell, and reads a's and b's cells, the clock, the scan
    # enable and its chain's scan in -- but not the chain's scan out, which only
    # it drives.
    assert by_label["y"]["core_net"] in by_direction["output"]
    assert by_label["a"]["core_net"] in by_direction["input"]
    assert by_label["b"]["core_net"] in by_direction["input"]
    ports = netlist["modules"][TOP]["ports"]
    for name in ("clk", "scan_en", "scan_in"):
        assert ports[name]["bits"][0] in by_direction["input"]
    assert ports["scan_out"]["bits"][0] in by_direction["output"]
    assert "inner" not in module["netnames"]
    # Its scan manifest: the wrapper chains alone, numbered from 0.
    graybox = box.manifest
    assert [c["index"] for c in graybox["chains"]] == [0, 1]
    assert box.chains == {0: 1, 1: 2}
    assert graybox["scan_inputs"] == ["wbr_si_0", "wbr_si_1"]
    assert {r["instance"] for r in graybox["cells"]} == {
        c["ff"] for c in wrapper["cells"]
    }
    assert {r["chain_index"] for r in graybox["cells"]} == {0, 1}
    assert graybox["max_chain_length"] == 2 and graybox["ineligible_ffs"] == []
    with pytest.raises(WrapError, match="already has a cell"):
        extest_graybox(box.netlist, manifest, CELL_MAP)


def test_extests_fault_sites_in_the_graybox_are_the_blocks(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    scanned, manifest = _scanned(tmp_path)
    box = extest_graybox(json.loads(scanned.read_text("utf-8")), manifest, CELL_MAP)
    graybox = tmp_path / "graybox.json"
    graybox.write_text(json.dumps(box.netlist), encoding="utf-8")
    rows = list(core.list_site_keys(str(graybox), str(CELL_MAP_PATH), "fail", [CORE]))
    owned = {
        str(row["site_key"])
        for row in rows
        if any(owner(row, t, manifest["wrapper"]) == EXTEST for t in FAULT_TYPES)
    }
    block = {
        str(row["site_key"])
        for row in core.list_site_keys(str(scanned), str(CELL_MAP_PATH), "fail", [])
    }
    assert owned and owned <= block
    # No fault of the core is in it.
    assert not any(":r0:" in str(row["site_key"]) for row in rows)


def test_a_socs_graybox_stubs_the_cells_tagged_with_each_block(
    tmp_path: Path,
) -> None:
    """On a SoC's record (faultflow.project.soc_wrapper), a block's core is the
    cells the composition tagged with its name, stubbed as <instance>__core -- not
    the cells whose names merely start like the instance's. With no cell tagged,
    the graybox is refused rather than keeping every core."""
    scanned, manifest = _scanned(tmp_path)
    netlist = json.loads(scanned.read_text(encoding="utf-8"))
    wrapper = manifest["wrapper"]
    blocks = [{"block": "A", "instance": "u"}]
    soc = {**manifest, "wrapper": {**wrapper, "blocks": blocks}}
    with pytest.raises(WrapError, match="name no block"):
        extest_graybox(netlist, soc, CELL_MAP)
    cells = netlist["modules"][TOP]["cells"]
    for cell in cells.values():
        cell.setdefault("attributes", {})["faultflow_block"] = "A"
    # Another block's cell, its instance's name starting like this one's.
    cells["u__x__g9"] = _cell("and2_1", A=3, B=4, X=99)
    cells["u__x__g9"]["attributes"]["faultflow_block"] = "B"
    box = extest_graybox(netlist, soc, CELL_MAP)
    soc_cells = box.netlist["modules"][TOP]["cells"]
    assert box.cores == ("u__core",)
    assert "u__x__g9" in soc_cells
    del cells["u__x__g9"]
    block = extest_graybox(netlist, manifest, CELL_MAP)
    block_cells = block.netlist["modules"][TOP]["cells"]
    assert set(soc_cells) - {"u__core", "u__x__g9"} == set(block_cells) - {CORE}
    assert soc_cells["u__core"]["connections"] == block_cells[CORE]["connections"]


def test_graybox_patterns_take_the_chips_chain_numbers() -> None:
    pattern = {
        "load_seqs": {"0": [True, False], "1": [False, True]},
        "expected_unload": {"0": [True, True], "1": [False, False]},
        "unload_mask": {"0": [True, False], "1": [True, True]},
        "launch_scan_in": {"1": True},
        "capture_pi_values": {"a": True},
    }
    (chip,) = chip_patterns([pattern], {0: 1, 1: 2}, 2)
    assert chip["load_seqs"] == {"1": [True, False], "2": [False, True]}
    assert chip["expected_unload"] == {"1": [True, True], "2": [False, False]}
    assert chip["unload_mask"] == {"1": [True, False], "2": [True, True]}
    assert chip["launch_scan_in"] == {"2": True}
    assert chip["shift_length"] == 2 and chip["capture_pi_values"] == {"a": True}
    with pytest.raises(WrapError, match="compressed"):
        chip_patterns([{**pattern, "seed": 3}], {0: 1, 1: 2}, 2)
