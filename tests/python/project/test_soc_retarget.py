"""A block's INTEST patterns retargeted onto its SoC (faultflow.retarget): where each
block chain sits on the SoC's chains, each pattern's bits placed there and its
inputs set through the SoC's; and on the SoC soc_v2_fixtures builds, each block's
retargeted patterns replayed on the composed SoC's cells, with every SoC input
they leave unset at X, and every credit of the block's INTEST reproduced there."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.control_trace import Trace
from faultflow.retarget import RetargetError, Segment, block_segments, retarget_pattern
from faultflow.scan.protocol import ScanPattern
from soc_v2_fixtures import write_soc_v2


def _chain(index: int, cells: list[str]) -> dict[str, Any]:
    return {"index": index, "cells": cells, "length": len(cells)}


# A's core chain is SoC chain 0; A's ring sits between two of B's ring cells on
# SoC chain 1.
SOC = {
    "chains": [
        _chain(0, ["u_a__f0", "u_a__f1", "u_a__f2"]),
        _chain(1, ["u_b__w0", "u_a__w0", "u_a__w1", "u_b__w1"]),
    ]
}
BLOCK = {"chains": [_chain(0, ["f0", "f1", "f2"]), _chain(1, ["w0", "w1"])]}


def test_each_block_chain_is_one_piece_of_a_soc_chain() -> None:
    assert block_segments(SOC, "u_a", BLOCK) == [
        Segment(0, 0, 0, 3),
        Segment(1, 1, 1, 2),
    ]
    reversed_ring = {"chains": [_chain(0, ["f0", "f1", "f2"]), _chain(1, ["w1", "w0"])]}
    with pytest.raises(RetargetError, match="isn't one piece"):
        block_segments(SOC, "u_a", reversed_ring)
    with pytest.raises(RetargetError, match="on none"):
        block_segments(SOC, "u_c", BLOCK)


def _block_pattern(**fields: Any) -> ScanPattern:
    """Chain 0 loads f0..f2 = 1, 0, 1; chain 1 (padded to 3) w0, w1 = 1, 0. They
    unload f0..f2 = 1, 1, 0 (f0 not compared) and w0, w1 = 0, 1."""
    given: dict[str, Any] = {
        "load_seqs": {0: [True, False, True], 1: [False, False, True]},
        "capture_pi_values": {"wbr_intest": True, "wbr_extest": False, "rst": True},
        "expected_unload": {0: [False, True, True], 1: [True, False, False]},
        "unload_mask": {0: [True, True, False], 1: [True, True, True]},
    }
    given.update(fields)
    return ScanPattern(**given)


def _retarget(pattern: ScanPattern, **options: Any) -> ScanPattern:
    given: dict[str, Any] = {
        "segments": block_segments(SOC, "u_a", BLOCK),
        "block_lengths": {0: 3, 1: 2},
        "soc_lengths": {0: 3, 1: 4},
        "inputs": {
            "rst": Trace("rst_a", None, True, ()),
            "tie": Trace(None, 1, False, ()),
            "mix": Trace(None, None, False, ()),
        },
        "outputs": {"y"},
        "mode_pins": {"wbr_intest": 1, "wbr_extest": 0},
        "holds": {"t_intest": 1, "t_extest": 0},
    }
    given.update(options)
    return retarget_pattern(pattern, 0, **given)


def test_a_block_patterns_bits_land_where_its_chains_sit() -> None:
    soc = _retarget(_block_pattern())
    # SoC chain 0 (3 of 4): A's core chain, padded before the load and after the
    # unload. SoC chain 1: A's ring at positions 1 and 2, the rest fill.
    assert soc.load_seqs == {
        0: [False, True, False, True],
        1: [False, False, True, False],
    }
    assert soc.expected_unload == {
        0: [False, True, True, False],
        1: [False, True, False, False],
    }
    assert soc.unload_mask == {
        0: [True, True, False, True],
        1: [False, True, True, False],
    }
    # rst reaches A through an inverter from rst_a; INTEST is the SoC's holds.
    assert soc.capture_pi_values == {
        "rst_a": False,
        "t_intest": True,
        "t_extest": False,
    }
    assert soc.launch == "" and soc.launch_scan_in == {} and soc.shift_length is None


def test_launching_on_shift_each_chain_head_takes_the_bit_before_it() -> None:
    soc = _retarget(_block_pattern(launch="los", launch_scan_in={0: True, 1: True}))
    # A's core chain heads SoC chain 0: the SoC chain's own launch bit. A's ring
    # follows B's first cell, which its load sets to the ring's launch bit.
    assert soc.launch == "los" and soc.launch_scan_in == {0: True}
    assert soc.load_seqs[1] == [False, False, True, True]
    # Behind another chain of the block, the launch bit is that chain's last bit.
    adjacent = {
        "chains": [_chain(0, ["u_a__f0", "u_a__f1", "u_a__f2", "u_a__w0", "u_a__w1"])]
    }
    options = {
        "segments": block_segments(adjacent, "u_a", BLOCK),
        "soc_lengths": {0: 5},
    }
    agreeing = _block_pattern(launch="los", launch_scan_in={1: True})
    assert _retarget(agreeing, **options).launch_scan_in == {0: False}
    with pytest.raises(RetargetError, match="launching on shift"):
        _retarget(_block_pattern(launch="los", launch_scan_in={1: False}), **options)


@pytest.mark.parametrize(
    "values, match",
    [
        ({"wbr_intest": False}, "doesn't hold wbr_intest"),
        ({"y": True}, "compares block output y"),
        ({"tie": False}, "ties to 1"),
        ({"mix": True}, "drives from logic"),
        ({"rst": False}, "INTEST holds at 1, at 0"),
        ({"rst": True, "rst2": True}, "at 0 and at 1"),
    ],
)
def test_a_pattern_the_soc_cant_apply_is_refused(
    values: dict[str, bool], match: str
) -> None:
    inputs = {
        "rst": Trace(
            "t_intest" if values == {"rst": False} else "rst_a", None, False, ()
        ),
        "rst2": Trace("rst_a", None, True, ()),
        "tie": Trace(None, 1, False, ()),
        "mix": Trace(None, None, False, ()),
    }
    with pytest.raises(RetargetError, match=match):
        _retarget(_block_pattern(capture_pi_values=values), inputs=inputs)
    # A tie the pattern agrees with costs nothing.
    assert _retarget(_block_pattern(capture_pi_values={"tie": True}), inputs=inputs)


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _retargeted_on_the_soc(tmp_path: Path, base: str) -> None:
    """The project run, then each block's INTEST patterns retargeted with ff.py
    project retarget: they replay on the composed SoC's cells, every SoC input
    they leave unset at X, and every credit of the block's INTEST is reproduced on
    the SoC with those inputs at 0 and at 1 -- or the credit is of a block input
    stem the SoC splits into branches, which has no SoC site."""
    from faultflow.cli import main
    from faultflow.config import load_config
    from faultflow.project.identity import merged_inputs, soc_identities
    from faultflow.project.manifest import load_soc_project
    from faultflow.project.soc_flow import (
        block_sites,
        project_output,
        run_soc_project,
    )
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.scan.tester_program import chip_of
    from faultflow.service.flow import FlowService
    from scan_credit import soc_credit_not_reproduced
    from scan_replay import replay_on_cells

    core = _load_core()
    assert core is not None
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        path = write_soc_v2(tmp_path, base=base)
        project = load_soc_project(path)
        service = FlowService()
        scopes = run_soc_project(
            project,
            load_config(project.base_config, project.soc.top),
            run_atpg=service.run_atpg,
            check_scan=service.check_scan,
            out=project_output(project),
        )
        soc = scopes.build.cfg
        manifest = json.loads(soc.scan_manifest_path.read_text("utf-8"))
        chip = chip_of(manifest)
        sites = block_sites(scopes)
        rows = core.list_site_keys(
            str(scopes.build.composed), str(resolve_scan_cell_map(soc)), "fail", []
        )
        identities = soc_identities(rows, sites)
        merged = merged_inputs(sites, identities)
        for instance, cfg in scopes.blocks.items():
            name = sites[instance].name
            dest = tmp_path / f"{name}_on_soc.json"
            command = ["project", "retarget", "-p", str(path), "--block", name]
            assert main([*command, "--out", str(dest)]) == 0
            patterns = json.loads(dest.read_text("utf-8"))
            assert patterns
            stil = tmp_path / f"{name}_on_soc.stil"
            command = ["project", "write-patterns", "-p", str(path)]
            assert main([*command, "--patterns", str(dest), "--out", str(stil)]) == 0
            assert stil.read_text("utf-8").startswith("STIL 1.0")
            fixed = {chip.scan_enable, *chip.scan_ins, *chip.clocks}
            unset = [
                bit
                for bit in chip.inputs
                if bit not in fixed
                and not any(bit in p["capture_pi_values"] for p in patterns)
            ]
            assert unset, "the other block's and the glue's inputs"
            replayed = replay_on_cells(
                soc, dest, tmp_path / f"replay_{name}", unknown_inputs=unset
            )
            assert replayed == [], name
            soc_sites: dict[str, set[str]] = {}
            for soc_key, owners in identities.items():
                for owner, key in owners:
                    if owner == name:
                        soc_sites.setdefault(key, set()).add(soc_key)
            missing = soc_credit_not_reproduced(cfg, soc, dest, soc_sites, unset)
            assert [
                credit for credit in missing if (name, credit[0]) not in merged
            ] == [], name


@pytest.mark.integration
def test_each_blocks_intest_patterns_run_on_the_soc(
    tmp_path: Path, flow_tools: None
) -> None:
    _retargeted_on_the_soc(tmp_path, "")


@pytest.mark.integration
def test_launch_on_shift_patterns_run_on_the_soc(
    tmp_path: Path, flow_tools: None
) -> None:
    _retargeted_on_the_soc(
        tmp_path,
        "[fault_model]\nmodel = transition\nlaunch = los\ncollapsing = false\n",
    )


def test_project_retarget_and_write_patterns_need_their_options(
    tmp_path: Path,
) -> None:
    from faultflow.cli import main

    project = ["-p", str(tmp_path / "project.json")]
    for command in (
        ["project", "retarget", *project],
        ["project", "retarget", *project, "--block", "blkA"],
        ["project", "write-patterns", *project, "--out", "x.stil"],
        ["project", *project, "--block", "blkA"],
        ["project", *project, "--patterns", "x.json"],
    ):
        with pytest.raises(SystemExit):
            main(command)
