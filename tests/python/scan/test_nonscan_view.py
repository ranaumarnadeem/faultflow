"""Non-scan cells in the scan test (faultflow.scan.nonscan): which flops a held input
forces, which hold still as X sources, and which holds are refused."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import stitch_scan_json
from faultflow.scan.atpg_view import (
    NONSCAN_X_ATTR,
    TIE0_CELL,
    TIE1_CELL,
    build_scan_atpg_view,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.nonscan import NonscanSetup, analyze_nonscan, jtag_sites
from faultflow.scan.reports import manifest_from_result
from faultflow.scan.x_mask import x_source_nets

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text())
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"
DFSTP = "sky130_fd_sc_hd__dfstp_1"
NONSCAN = ("u_tap__free", "u_tap__set", "u_tap__st")


def _flop(cell_type: str, **pins: int) -> dict[str, Any]:
    return {
        "type": cell_type,
        "port_directions": {p: "output" if p == "Q" else "input" for p in pins},
        "connections": {p: [net] for p, net in pins.items()},
    }


def _module() -> dict[str, Any]:
    """u0 scans on clk; the three u_tap__* flops run on tck: cleared by trst_n,
    preset by it, and with no reset at all."""
    ports = {"clk": 2, "d": 3, "tck": 5, "trst_n": 6, "tdi": 7, "scan_en": 11}
    outputs = {"q": 4, "tdo": 8, "s": 9, "f": 10}
    return {
        "ports": {
            **{n: {"direction": "input", "bits": [b]} for n, b in ports.items()},
            **{n: {"direction": "output", "bits": [b]} for n, b in outputs.items()},
        },
        "cells": {
            "u0": _flop(DFXTP, CLK=2, D=3, Q=4),
            "u_tap__st": _flop(DFRTP, CLK=5, D=7, RESET_B=6, Q=8),
            "u_tap__set": _flop(DFSTP, CLK=5, D=7, SET_B=6, Q=9),
            "u_tap__free": _flop(DFXTP, CLK=5, D=7, Q=10),
        },
    }


def _analyze(
    holds: list[tuple[str, int]], scan_reset_holds: dict[str, bool] | None = None
) -> NonscanSetup:
    manifest = {
        "clock_nets": [2],
        "scan_inputs": ["scan_in_0"],
        "scan_enable": "scan_en",
        "ineligible_ffs": [
            {"instance": name, "cell_type": "x", "reason": "nonscan_policy"}
            for name in NONSCAN
        ],
    }
    return analyze_nonscan(
        _module(),
        manifest,
        CELL_MAP,
        globs=("u_tap__*",),
        holds=holds,
        scan_reset_holds=scan_reset_holds or {},
    )


@pytest.mark.unit
def test_held_in_reset_a_flop_is_forced_and_a_frozen_one_is_an_x_source() -> None:
    setup = _analyze([("trst_n", 0), ("tck", 0)])

    values = {flop.instance: flop.value for flop in setup.flops}
    assert values == {"u_tap__st": 0, "u_tap__set": 1, "u_tap__free": None}
    assert setup.x_sources == (10,)
    assert setup.holds == {"trst_n": 0, "tck": 0}
    st = next(flop for flop in setup.flops if flop.instance == "u_tap__st")
    assert st.inputs == (("CLK", 5), ("D", 7), ("RESET_B", 6))
    # trst_n stuck at 1 would release them: scan leaves that fault to JTAG.
    assert ("net:6:stem", "sa1") in setup.release_faults
    assert setup.owns("u_tap__$abc$12") and not setup.owns("u0")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("holds", "message"),
    [
        ([("tck", 0)], "preset isn't held, so a scan pattern could change it"),
        ([("trst_n", 0)], "its clock isn't held"),
        ([("trst_n", 0), ("tck", 0), ("tdo", 0)], "not a single-bit input"),
        ([("trst_n", 0), ("tck", 0), ("clk", 0)], "a scan clock can't be held"),
        ([("trst_n", 0), ("tck", 0), ("scan_en", 0)], "a scan port can't be held"),
    ],
)
def test_a_nonscan_flop_left_free_or_a_hold_that_breaks_scan_is_refused(
    holds: list[tuple[str, int]], message: str
) -> None:
    with pytest.raises(ScanError, match=message):
        _analyze(holds)


def _stitched(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], NonscanSetup]:
    """The same four flops as a netlist, stitched with the u_tap__* flops left out,
    and its non-scan setup: trst_n and tck held at 0. u_tap__dead reads tdi and
    drives a net nothing reads."""
    ports = {"clk": 2, "d": 3, "tck": 5, "trst_n": 6, "tdi": 7}
    outputs = {"q": 4, "tdo": 8, "s": 9, "f": 10}
    module = _module()
    module["cells"]["u_tap__dead"] = {
        "type": "sky130_fd_sc_hd__inv_1",
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [7], "Y": [12]},
    }
    module["ports"] = {
        **{n: {"direction": "input", "bits": [b]} for n, b in ports.items()},
        **{n: {"direction": "output", "bits": [b]} for n, b in outputs.items()},
    }
    for cell in module["cells"].values():
        cell.update(hide_name=0, parameters={}, attributes={})
    module["attributes"] = {"top": "1"}
    module["netnames"] = {
        name: {"hide_name": 0, "bits": [net], "attributes": {}}
        for name, net in {**ports, **outputs}.items()
    }
    source = tmp_path / "mini.json"
    source.write_text(json.dumps({"modules": {"mini": module}}), encoding="utf-8")
    result = stitch_scan_json(
        source,
        CELL_MAP_PATH,
        "mini",
        tmp_path / "scan.json",
        nonscan_cells=("u_tap__*",),
    )
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)
    stitched = json.loads((tmp_path / "scan.json").read_text(encoding="utf-8"))
    setup = analyze_nonscan(
        stitched["modules"]["mini"],
        manifest,
        CELL_MAP,
        globs=("u_tap__*",),
        holds=[("trst_n", 0), ("tck", 0)],
        scan_reset_holds={},
    )
    return stitched, manifest, setup


@pytest.mark.unit
def test_the_view_ties_the_nonscan_flops_and_the_held_inputs(tmp_path: Path) -> None:
    stitched, manifest, setup = _stitched(tmp_path)

    view, _ = build_scan_atpg_view(stitched, manifest, nonscan=setup)

    module = view["modules"]["mini"]
    cells, ports = module["cells"], module["ports"]
    assert not set(NONSCAN) & set(cells)

    def tie(name: str) -> tuple[str, list[int]]:
        return cells[name]["type"], cells[name]["connections"]["Y"]

    assert tie("$nstie0_u_tap__st") == (TIE0_CELL, [8])
    assert tie("$nstie1_u_tap__set") == (TIE1_CELL, [9])
    assert tie("$nsx_u_tap__free") == (TIE0_CELL, [10])
    assert cells["$nsx_u_tap__free"]["attributes"][NONSCAN_X_ATTR] == "1"
    assert x_source_nets(module, ()) == [10]
    # Each input pin keeps a reader; each held input's port is gone, a tie drives it.
    assert {n for n in cells if n.startswith("$nssink_u_tap__st_")} == {
        "$nssink_u_tap__st_CLK",
        "$nssink_u_tap__st_D",
        "$nssink_u_tap__st_RESET_B",
    }
    assert cells["$nssink_u_tap__st_RESET_B"]["connections"]["A"] == [6]
    assert not {"trst_n", "tck"} & set(ports) and "tdi" in ports
    assert tie("$nshold_trst_n") == (TIE0_CELL, [6])
    assert tie("$nshold_tck") == (TIE0_CELL, [5])


@pytest.mark.unit
def test_jtag_sites_are_the_nonscan_logic_and_what_only_it_sees(tmp_path: Path) -> None:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    if core is None:
        pytest.skip("needs the C++ core")
    stitched, _, setup = _stitched(tmp_path)
    rows = list(
        core.list_site_keys(str(tmp_path / "scan.json"), str(CELL_MAP_PATH), "fail", [])
    )

    sites = jtag_sites(rows, stitched["modules"]["mini"], CELL_MAP, setup, tdo="tdo")

    # The u_tap__* flops' outputs (tdo, s, f) are theirs; tck, trst_n and tdi reach
    # nothing else. d feeds the scan flop u0: scan's. u_tap__dead's input branch and
    # its unread output reach nothing: scan's, to prove redundant.
    keys = {str(r["site_key"]) for r in rows}
    by_net = {int(key.split(":")[1]) for key in keys}
    assert {3, 4, 12} <= by_net
    dead = {"net:7:branch:u_tap__dead:A"} | {k for k in keys if k.startswith("net:12:")}
    assert dead <= keys
    assert (
        sites
        == {key for key in keys if int(key.split(":")[1]) in {5, 6, 7, 8, 9, 10}} - dead
    )


@pytest.mark.unit
def test_a_hold_that_resets_scan_flops_is_refused() -> None:
    """Scan flops cleared by trst_n need it inactive (1) during the test."""
    with pytest.raises(ScanError, match="would clear or preset scan flops"):
        _analyze([("trst_n", 0), ("tck", 0)], scan_reset_holds={"trst_n": True})
