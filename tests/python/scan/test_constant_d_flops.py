"""Flops whose D synthesis tied to a constant are scanned like any other.

Yosys can prove a flop's next state constant after abc (s5378, s13207, s15850):
the flop keeps a literal ``"0"``/``"1"`` on D. It is still a flop -- a set or reset
can make it hold the other value, and its Q drives logic -- so it goes on a chain.
Its scan cell keeps the literal on D; the core keys the constant as net -1 (``"0"``)
or -2 (``"1"``), and a fault on the constant's branch into D is graded where the
flop captures it.

The chip (sky130, before scan): clk 2, rst_n 3, d0 4, d1 5;
u_s0 dfstp D="0" (the ISCAS shape), u_r0 dfrtp D="0", u_x1 dfxtp D="1", and u_n,
a plain flop on d0 & u_s0.Q. y = u_r0 ^ u_x1, z = u_n | d1.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from scan_credit import credit_not_reproduced

from faultflow.config import load_config
from faultflow.scan.stitch import plan_scan_json, stitch_scan_json

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "chip"
# The core's net ids for the two constants (parsed_graph.cpp: parse_bit).
CONST0, CONST1 = -1, -2
SECTIONS = (
    "[fault_model]\ntype = stuck_at\ncollapsing = false\n\n"
    "[simulation]\nunsupported_cells = fail\nverify = false\n\n"
    "[atpg]\ntool = native\nworkers = 1\n\n"
    "[report]\nthreshold = 100.0\n"
)


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    outputs = ("Q", "Q_N", "X")
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}_1",
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _chip(**replace: dict[str, Any]) -> dict[str, Any]:
    cells = {
        "u_s0": _cell("dfstp", CLK=2, D="0", SET_B=3, Q=10),
        "u_r0": _cell("dfrtp", CLK=2, D="0", RESET_B=3, Q=11),
        "u_x1": _cell("dfxtp", CLK=2, D="1", Q=12),
        "g_d": _cell("and2", A=4, B=10, X=13),
        "u_n": _cell("dfxtp", CLK=2, D=13, Q=14),
        "g_y": _cell("xor2", A=11, B=12, X=15),
        "g_z": _cell("or2", A=14, B=5, X=16),
    }
    cells.update(replace)
    inputs = {"clk": 2, "rst_n": 3, "d0": 4, "d1": 5}
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in inputs.items()},
        "y": {"direction": "output", "bits": [15]},
        "z": {"direction": "output", "bits": [16]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {
        n: {"hide_name": 0, "bits": p["bits"]} for n, p in ports.items()
    }
    return {"modules": {TOP: module}}


def _write(tmp_path: Path, chip: dict[str, Any]) -> Path:
    netlist = tmp_path / "chip.json"
    netlist.write_text(json.dumps(chip), encoding="utf-8")
    return netlist


@pytest.mark.unit
def test_a_flop_with_a_constant_d_is_scan_eligible(tmp_path: Path) -> None:
    plan = plan_scan_json(_write(tmp_path, _chip()), CELL_MAP_PATH, TOP)

    assert plan.ineligible_ffs == []
    assert sorted(cell.instance for cell in plan.cells) == [
        "u_n",
        "u_r0",
        "u_s0",
        "u_x1",
    ]


@pytest.mark.unit
def test_its_scan_cell_keeps_the_constant_on_d(tmp_path: Path) -> None:
    out = tmp_path / "scan.json"
    result = stitch_scan_json(_write(tmp_path, _chip()), CELL_MAP_PATH, TOP, out)

    cells = json.loads(out.read_text(encoding="utf-8"))["modules"][TOP]["cells"]
    assert cells["u_s0"]["connections"]["D"] == ["0"]
    assert cells["u_r0"]["connections"]["D"] == ["0"]
    assert cells["u_x1"]["connections"]["D"] == ["1"]
    assert cells["u_n"]["connections"]["D"] == [13]
    # The record names the constant as the core does.
    data = {cell.instance: cell.data_net for cell in result.cells}
    assert data == {"u_s0": CONST0, "u_r0": CONST0, "u_x1": CONST1, "u_n": 13}


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


def _run(work: Path, chip: dict[str, Any], steps: list[list[str]]) -> list[int]:
    """Each CLI step's exit code, run in ``work`` (the current directory: the
    config's output paths are relative to it)."""
    from faultflow.cli import main

    netlist = _write(work, chip)
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ntop = {TOP}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n{SECTIONS}",
        encoding="utf-8",
    )
    codes = []
    for step in steps:
        try:
            codes.append(main([*step, "--top", TOP, "-c", str(ofs)]))
        except SystemExit as exc:  # an error exits instead of returning
            codes.append(int(exc.code or 0))
    return codes


def _faults(cfg: Any) -> dict[tuple[str, str], str]:
    """(site key, fault type) -> status, for the latest scan campaign."""
    conn = sqlite3.connect(cfg.db_path)
    (campaign,) = conn.execute(
        "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
    ).fetchone()
    rows = conn.execute(
        "SELECT fault_site_key, fault_type, status FROM faults WHERE campaign_id = ?",
        (campaign,),
    ).fetchall()
    conn.close()
    return {(key, kind): status for key, kind, status in rows}


def _constant_d_site(keys: set[str], instance: str, net: int) -> str:
    """The constant's site into ``instance``'s D: a branch when the constant has
    other readers, else the constant's stem."""
    branch = f"net:{net}:branch:{instance}:D"
    return branch if branch in keys else f"net:{net}:stem"


@pytest.mark.integration
def test_constant_d_flops_are_scanned_and_graded(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    patterns = tmp_path / "patterns.json"
    codes = _run(
        tmp_path,
        _chip(),
        [
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(patterns)],
        ],
    )
    assert codes == [0, 0, 0, 0]
    cfg = load_config(tmp_path / "chip.ofs", TOP)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    assert manifest["ineligible_ffs"] == []

    # Every credit is one the full scan protocol reproduces (CLAUDE.md #24).
    assert credit_not_reproduced(cfg, patterns) == []

    faults = _faults(cfg)
    keys = {key for key, _ in faults}
    for instance, q in (("u_s0", 10), ("u_r0", 11), ("u_x1", 12)):
        # Its Q drives logic, and a scan load sets it to either value.
        assert faults[(f"net:{q}:stem", "sa0")] == "detected", instance
        assert faults[(f"net:{q}:stem", "sa1")] == "detected", instance
    # The constant's fault into D: flipping it changes what the flop captures,
    # holding it at its own value changes nothing.
    for instance, net, flips, holds in (
        ("u_s0", CONST0, "sa1", "sa0"),
        ("u_r0", CONST0, "sa1", "sa0"),
        ("u_x1", CONST1, "sa0", "sa1"),
    ):
        site = _constant_d_site(keys, instance, net)
        assert faults[(site, flips)] == "detected", (instance, site)
        assert faults[(site, holds)] == "redundant", (instance, site)


@pytest.mark.integration
def test_constant_d_patterns_replay_on_real_cells(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scan_replay import replay_on_cells

    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")
    monkeypatch.chdir(tmp_path)
    patterns = tmp_path / "patterns.json"
    codes = _run(
        tmp_path,
        _chip(),
        [
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(patterns)],
        ],
    )
    assert codes == [0, 0, 0, 0]
    cfg = load_config(tmp_path / "chip.ofs", TOP)
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
def test_scan_check_refuses_a_flop_sim_would_refuse(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flop with both a reset and a set has no sky130 scan cell: sim --scan
    refuses the campaign, so scan-check must not pass it first."""
    monkeypatch.chdir(tmp_path)
    chip = _chip(
        u_n=_cell("dfbbp", CLK=2, D=13, RESET_B=3, SET_B=5, Q=14),
    )
    codes = _run(tmp_path, chip, [["init"], ["scan"], ["scan-check"]])

    assert codes[:2] == [0, 0]
    assert codes[2] != 0
    cfg = load_config(tmp_path / "chip.ofs", TOP)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    latest = manifest["latest_check"]
    assert latest["status"] == "FAIL"
    assert any("u_n" in error for error in latest["errors"])
