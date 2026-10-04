"""Settled non-scan flops: a reset synchronizer a scan clock keeps clocking, its chip
reset held inactive, reaches a known value a few pulses into the test. Scan ties it
there, starts every pattern with that many pulses (the preamble), and counts its own
faults as reset faults.

The chip: u_rst_sync__s0 -> u_rst_sync__s1, a two-flop synchronizer on clk cleared by
rst_n, resets r0, the one scan flop -- a chain shorter than the synchronizer, so only
the preamble settles it before the capture."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.scan.errors import ScanError
from faultflow.scan.nonscan import analyze_nonscan, jtag_sites, settled_sites
from faultflow.scan.pattern_export import scan_pattern_from_dict, scan_pattern_to_dict
from faultflow.scan.protocol import ScanPattern
from scan_credit import credit_not_reproduced

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
TOP = "sync_chip"
SYNC = ("u_rst_sync__s0", "u_rst_sync__s1")
DFRTP = "sky130_fd_sc_hd__dfrtp_1"


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {
            p: "output" if p in ("Q", "X", "Y") else "input" for p in conns
        },
        "connections": {p: [n] for p, n in conns.items()},
    }


def _chip(**overrides: dict[str, Any]) -> dict[str, Any]:
    """clk 2, rst_n 3, d0 4, d1 5. s0 (D tied 1) -> s1 on clk, cleared by rst_n;
    r0 (D = d0 ^ d1) on clk, cleared by s1. q0 = r0, y = r0 & d1."""
    ports = {"clk": 2, "rst_n": 3, "d0": 4, "d1": 5}
    outputs = {"q0": 12, "y": 15}
    cells = {
        "u_rst_sync__s0": _cell(DFRTP, CLK=2, D="1", RESET_B=3, Q=10),
        "u_rst_sync__s1": _cell(DFRTP, CLK=2, D=10, RESET_B=3, Q=11),
        "g_x": _cell("sky130_fd_sc_hd__xor2_1", A=4, B=5, X=14),
        "r0": _cell(DFRTP, CLK=2, D=14, RESET_B=11, Q=12),
        "g_y": _cell("sky130_fd_sc_hd__and2_1", A=12, B=5, X=15),
    }
    cells.update(overrides)
    nets = {**ports, **outputs, "sync_mid": 10, "sync_out": 11, "x": 14}
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": {
                    **{
                        n: {"direction": "input", "bits": [b]} for n, b in ports.items()
                    },
                    **{
                        n: {"direction": "output", "bits": [b]}
                        for n, b in outputs.items()
                    },
                },
                "cells": cells,
                "netnames": {
                    name: {"hide_name": 0, "bits": [bit], "attributes": {}}
                    for name, bit in nets.items()
                },
            }
        }
    }


def _manifest(nonscan: tuple[str, ...] = SYNC) -> dict[str, Any]:
    return {
        "clock_nets": [2],
        "scan_inputs": ["scan_in_0"],
        "scan_enable": "scan_en",
        "ineligible_ffs": [
            {"instance": name, "cell_type": DFRTP, "reason": "nonscan_policy"}
            for name in nonscan
        ],
    }


def _analyze(
    design: dict[str, Any],
    holds: list[tuple[str, int]],
    globs: tuple[str, ...] = ("u_rst_sync__*",),
    nonscan: tuple[str, ...] = SYNC,
) -> Any:
    return analyze_nonscan(
        design["modules"][TOP],
        _manifest(nonscan),
        CELL_MAP,
        globs=globs,
        holds=holds,
        scan_reset_holds={},
    )


@pytest.mark.unit
def test_a_reset_synchronizer_settles_in_two_pulses() -> None:
    setup = _analyze(_chip(), [("rst_n", 1)])
    by_name = {f.instance: f for f in setup.flops}
    assert [(by_name[n].value, by_name[n].settle) for n in SYNC] == [(1, 1), (1, 2)]
    assert setup.preamble == 2 and setup.x_sources == ()
    assert not setup.owns("u_rst_sync__s0")  # not JTAG's


@pytest.mark.unit
def test_held_in_reset_it_is_forced_not_settled() -> None:
    setup = _analyze(_chip(), [("rst_n", 0)])
    assert {f.instance: (f.value, f.settle) for f in setup.flops} == {
        name: (0, 0) for name in SYNC
    }
    assert setup.preamble == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        # D from a free input: it follows d0.
        ({"u_rst_sync__s0": _cell(DFRTP, CLK=2, D=4, RESET_B=3, Q=10)}, "settles it"),
        # Clocked by d1, no scan clock: nothing pulses it in the preamble.
        ({"u_rst_sync__s0": _cell(DFRTP, CLK=5, D="1", RESET_B=3, Q=10)}, "settles it"),
    ],
    ids=["d_from_a_free_input", "clock_not_a_scan_clock"],
)
def test_a_flop_that_doesnt_settle_is_refused(
    overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(ScanError, match=match):
        _analyze(_chip(**overrides), [("rst_n", 1)])


@pytest.mark.unit
def test_a_glob_selecting_settled_and_jtag_flops_is_refused() -> None:
    """t0, a TCK flop held in reset by trst_n, runs non-scan beside the
    synchronizer; one glob for both is refused, two are fine."""
    design = _chip(u_rst_sync__t0=_cell(DFRTP, CLK=6, D=4, RESET_B=7, Q=16))
    ports = design["modules"][TOP]["ports"]
    ports.update(
        {
            n: {"direction": "input", "bits": [b]}
            for n, b in {"tck": 6, "trst_n": 7}.items()
        }
    )
    nonscan = (*SYNC, "u_rst_sync__t0")
    holds = [("rst_n", 1), ("trst_n", 0), ("tck", 0)]
    with pytest.raises(ScanError, match="give each its own glob"):
        _analyze(design, holds, nonscan=nonscan)
    setup = _analyze(
        design, holds, globs=("u_rst_sync__s*", "u_rst_sync__t*"), nonscan=nonscan
    )
    assert setup.owns("u_rst_sync__t0") and not setup.owns("u_rst_sync__s1")


@pytest.mark.unit
def test_its_own_sites_are_reset_faults_and_none_are_jtags(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    design = _chip()
    netlist = tmp_path / "chip.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    core = _load_core()
    assert core is not None
    rows = list(core.list_site_keys(str(netlist), str(CELL_MAP_PATH), "fail", []))
    setup = _analyze(design, [("rst_n", 1)])
    sites = settled_sites(rows, setup)
    # s0 -> s1 is fanout-free: one stem, both s0's output and s1's D pin; s0's D,
    # tied to 1, is the constant's only reader.
    assert {"net:10:stem", "net:-2:stem"} <= sites
    assert {"net:11:stem", "net:3:branch:u_rst_sync__s0:RESET_B"} <= sites
    assert "net:11:branch:r0:RESET_B" not in sites  # r0's pin: r0's fault
    module = design["modules"][TOP]
    assert not jtag_sites(rows, module, CELL_MAP, setup, tdo="tdo") & sites


@pytest.mark.unit
def test_a_pattern_keeps_its_preamble_through_the_file() -> None:
    pattern = ScanPattern({0: [True]}, {"rst_n": True}, {0: [False]})
    assert "preamble_cycles" not in scan_pattern_to_dict(pattern)
    settled = ScanPattern({0: [True]}, {"rst_n": True}, {0: [False]}, preamble_cycles=2)
    data = json.loads(json.dumps(scan_pattern_to_dict(settled)))
    assert scan_pattern_from_dict(data) == settled


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    """init -> scan -> scan-check -> sim --scan, the synchronizer non-scan."""
    from faultflow.cli import main
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    work = tmp_path_factory.mktemp("settled")
    netlist = work / "chip.json"
    netlist.write_text(json.dumps(_chip()), encoding="utf-8")
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n\n"
        "[scan]\nchains = 1\nnonscan_cells = u_rst_sync__*\nhold = rst_n:1\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(work)
        for step in (
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(work / "patterns.json")],
        ):
            assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
        return work, load_config(ofs, TOP)


@pytest.mark.integration
def test_scan_settles_the_synchronizer_and_replays_with_the_preamble(
    scanned: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, cfg = scanned
    monkeypatch.chdir(work)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    assert sorted(ff["instance"] for ff in manifest["ineligible_ffs"]) == list(SYNC)
    patterns = json.loads((work / "patterns.json").read_text(encoding="utf-8"))
    assert patterns and {p["preamble_cycles"] for p in patterns} == {2}

    with sqlite3.connect(cfg.db_path) as conn:
        exclusion = dict(
            conn.execute(
                "SELECT fault_site_key, exclusion FROM faults "
                "WHERE fault_site_key = 'net:10:stem'"
            ).fetchall()
        )
    assert exclusion == {"net:10:stem": "reset"}

    # Every fault scan credits reproduces in the full protocol after the
    # preamble; without it, r0 is still in reset at the capture.
    assert credit_not_reproduced(cfg, work / "patterns.json") == []
    assert credit_not_reproduced(cfg, work / "patterns.json", preamble=0)
