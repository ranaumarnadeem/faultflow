"""Flops with a second, inverted output (Sky130's dfxbp, dfrbp, sdfxbp, ...; the
generated cell map gives them outputs {"Q", "QN"}). The engine models Q_N as an inverter
reading Q, so its net has a driver, a value and fault sites like any other; scan
stitching, which replaces a flop by its own scan flop driving Q alone, leaves a flop
whose Q_N is in use out of the chains rather than leave that net undriven."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.pdk.command import cell_map_command

ROOT = Path(__file__).resolve().parents[3]
HD_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
CLK, D, B, Q, QN, Y, Q2 = 2, 3, 4, 5, 6, 7, 8


@pytest.fixture(scope="module")
def cell_map(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    out = tmp_path_factory.mktemp("map") / "hd.json"
    cell_map_command(HD_LIB, out)
    entry = json.loads(out.read_text(encoding="utf-8"))["sky130_fd_sc_hd__dfxbp_*"]
    assert entry["outputs"] == {"Q": "Q", "QN": "Q_N"}
    return out


def _cell(kind: str, conns: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {},
        "connections": conns,
    }


def _netlist(path: Path, *, qn_used: bool) -> Path:
    """dfxbp: D -> (Q, Q_N); Y = Q_N & B when qn_used, else Q_N dangles."""
    cells = {
        "u_ff": _cell(
            "sky130_fd_sc_hd__dfxbp_1",
            {"CLK": [CLK], "D": [D], "Q": [Q], "Q_N": [QN]},
        ),
        # An ordinary flop, so a scan stitch always has one to stitch.
        "u_other": _cell(
            "sky130_fd_sc_hd__dfxtp_1", {"CLK": [CLK], "D": [B], "Q": [Q2]}
        ),
    }
    ports = {
        "CLK": {"direction": "input", "bits": [CLK]},
        "D": {"direction": "input", "bits": [D]},
        "B": {"direction": "input", "bits": [B]},
        "Q": {"direction": "output", "bits": [Q]},
        "Q2": {"direction": "output", "bits": [Q2]},
    }
    if qn_used:
        cells["u_and"] = _cell(
            "sky130_fd_sc_hd__and2_1", {"A": [QN], "B": [B], "X": [Y]}
        )
        ports["QN"] = {"direction": "output", "bits": [QN]}
        ports["Y"] = {"direction": "output", "bits": [Y]}
    module = {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": {},
    }
    path.write_text(json.dumps({"modules": {"top": module}}), encoding="utf-8")
    return path


def test_q_n_is_the_inverse_of_q_and_drives_its_readers(
    tmp_path: Path, cell_map: Path
) -> None:
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    netlist = _netlist(tmp_path / "top.json", qn_used=True)

    def pulse(d: bool, b: bool) -> list[dict[str, bool]]:
        return [{"CLK": False, "D": d, "B": b}, {"CLK": True, "D": d, "B": b}]

    results = core.fault_free_sequence_outputs(
        str(netlist),
        str(cell_map),
        [
            pulse(True, True),
            pulse(True, True) + pulse(False, True),
            pulse(False, False),
        ],
        ["CLK", "D", "B"],
        ["Q", "QN", "Y"],
        "fail",
    )
    assert results[0] == {"Q": True, "QN": False, "Y": False}
    assert results[1] == {"Q": False, "QN": True, "Y": True}
    assert results[2] == {"Q": False, "QN": True, "Y": False}


def test_q_n_has_fault_sites_of_its_own(tmp_path: Path, cell_map: Path) -> None:
    from faultflow.runner.runner import _load_core

    core: Any = _load_core()
    netlist = _netlist(tmp_path / "top.json", qn_used=True)
    rows = core.list_site_keys(str(netlist), str(cell_map), "fail", [])
    keys = {row["site_key"] for row in rows}
    assert f"net:{QN}:stem" in keys
    assert f"net:{Q}:stem" in keys


@pytest.mark.parametrize(("qn_used", "scanned"), [(True, False), (False, True)])
def test_scan_leaves_a_flop_whose_q_n_is_in_use_out(
    tmp_path: Path, cell_map: Path, qn_used: bool, scanned: bool
) -> None:
    from faultflow.scan import stitch_scan_json

    netlist = _netlist(tmp_path / "top.json", qn_used=qn_used)
    result = stitch_scan_json(netlist, cell_map, "top", tmp_path / "scan.json")
    reasons = {ff.instance: ff.reason for ff in result.ineligible_ffs}
    if scanned:
        assert "u_ff" not in reasons
    else:
        assert reasons["u_ff"] == "inverted_output_used"
