"""A fault scan can't test only because of [scan] hold -- a held input, or a non-scan
flop it keeps in reset, blocks every path -- is hold_unresolved (Tessent's AU.PC):
undetected, counted in the denominator, never retried. A fault only the holds and a
blackbox together block is blackbox_unresolved, and only a fault untestable whatever
both do stays redundant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.db import connect, init_schema, summary
from faultflow.db.campaign import ensure_campaign
from faultflow.runner.progressive_atpg import redundancy_model_id
from faultflow.scan.atpg_view import (
    NONSCAN_FREE_PORT_PREFIX,
    NONSCAN_OBSERVE_PORT_PREFIX,
    build_scan_atpg_view,
    make_nonscan_free,
)
from faultflow.scan.detection_pipeline import (
    build_scan_pipeline_context,
    run_progressive_scan_atpg,
)
from faultflow.scan.x_mask import x_source_nets

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "mini"


def _cell(kind: str, **conns: list[int | str]) -> dict[str, Any]:
    outputs = {"Q", "X", "dout"}
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": conns,
    }


def _mini(with_memory: bool) -> dict[str, Any]:
    """u0 scans on CLK. u_tap__st, a TAP flop on TCK cleared by TRST_N, is non-scan
    and held in reset, so its Q (net 11) is 0 in every scan pattern. Y = Q & A, so
    testing A or Y through Y needs Q = 1. Z = A | 1, which nothing can test through.

    With the memory: W = dout & Q, a point dout's unknown value reaches, so a test
    through W needs the memory's output and Q both; MDIN is read by the memory
    alone."""
    ports = {"CLK": 2, "D": 3, "A": 5, "TRST_N": 6, "TCK": 9, "TDI": 10}
    outputs = {"Q": 4, "Y": 7, "Z": 12}
    cells = {
        "u0": _cell("sky130_fd_sc_hd__dfxtp_1", CLK=[2], D=[3], Q=[4]),
        "u_tap__st": _cell(
            "sky130_fd_sc_hd__dfrtp_1", CLK=[9], D=[10], RESET_B=[6], Q=[11]
        ),
        "g_and": _cell("sky130_fd_sc_hd__and2_1", A=[11], B=[5], X=[7]),
        "g_or": _cell("sky130_fd_sc_hd__or2_1", A=[5], B=["1"], X=[12]),
    }
    if with_memory:
        ports["MDIN"] = 15
        outputs["W"] = 14
        cells["u_mem"] = _cell("sram_like", din=[15], dout=[13])
        cells["g_both"] = _cell("sky130_fd_sc_hd__and2_1", A=[13], B=[11], X=[14])
    return {
        "attributes": {"top": "1"},
        "ports": {
            **{n: {"direction": "input", "bits": [b]} for n, b in ports.items()},
            **{n: {"direction": "output", "bits": [b]} for n, b in outputs.items()},
        },
        "cells": cells,
        "netnames": {
            name: {"hide_name": 0, "bits": [bit], "attributes": {}}
            for name, bit in {**ports, **outputs}.items()
        },
    }


def _workspace(
    tmp_path: Path, *, with_memory: bool, launch_mode: str | None
) -> tuple[Any, Path, Any, dict[str, object]]:
    from faultflow.config import load_config
    from faultflow.runner import Runner
    from faultflow.runner.runner import _load_core, _port_names
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp
    from faultflow.scan.x_mask import mask_scan_view

    source = tmp_path / "mini.json"
    source.write_text(
        json.dumps({"modules": {TOP: _mini(with_memory)}}), encoding="utf-8"
    )
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""[design]
netlist = {source}
cell_lib = {CELL_MAP}
[fault_model]
collapsing = false
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
random_vectors = 0
max_rounds = 3
[scan]
nonscan_cells = u_tap__*
hold = TRST_N:0
""" + ("[blackbox]\ninstances = u_mem\n" if with_memory else ""),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, TOP)
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test\n", encoding="utf-8")
    result = stitch_scan_json(
        source, CELL_MAP, TOP, generic, nonscan_cells=cfg.scan.nonscan_cells
    )
    manifest = manifest_from_result(result, source, techmap, None)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": hash_file(generic),
    }
    cfg.scan_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    nonscan = Runner(cfg)._nonscan_setup(manifest, generic)
    assert nonscan is not None
    view, pseudo_port_map = build_scan_atpg_view(
        json.loads(generic.read_text(encoding="utf-8")),
        manifest,
        blackbox_instances=cfg.blackbox_instances,
        nonscan=nonscan,
    )
    atpg_view = cfg.intermediate_dir / "scan_atpg_view.json"
    atpg_view.write_text(json.dumps(view), encoding="utf-8")
    functional_outputs = [
        port
        for port in _port_names(atpg_view, cfg.top, "output")
        if not port.startswith("__ppo_")
    ]
    x_mask = None
    if with_memory:
        x_mask = mask_scan_view(
            _load_core(),
            cfg,
            view,
            atpg_view,
            manifest,
            generic,
            functional_outputs,
            launch_mode=launch_mode,
            nonscan_q_nets=[flop.q for flop in nonscan.flops],
        )
    scan_ctx = build_scan_pipeline_context(
        cfg,
        manifest,
        generic,
        pseudo_port_map,
        functional_outputs,
        x_mask=x_mask,
        nonscan=nonscan,
    )
    fp: dict[str, object] = {
        "top": cfg.top,
        "netlist_hash": "scan-test",
        "cell_lib_hash": "cell-test",
        "config_hash": "cfg-test",
        "template_hash": "tmpl-test",
        "yosys_version": "yosys",
        "faultflow_version": "test",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
        "manifest_hash": str(manifest.get("generic_json_hash", "")),
        "atpg_view_schema_ver": "scan-atpg-view-observe-buf-1",
    }
    return cfg, atpg_view, scan_ctx, fp


def _run(
    tmp_path: Path, *, with_memory: bool, launch_mode: str | None
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, Any], Any]:
    """Each fault site's class after the campaign -- hold, blackbox, redundant,
    jtag -- and the summary and ATPG stats."""
    cfg, atpg_view, scan_ctx, fp = _workspace(
        tmp_path, with_memory=with_memory, launch_mode=launch_mode
    )
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)
    _vectors, stats, *_rest = run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=3,
        target_coverage=100.0,
        transition=launch_mode is not None,
        launch_mode=launch_mode or "loc",
    )
    classes: dict[str, set[tuple[str, str]]] = {
        "hold": set(),
        "blackbox": set(),
        "redundant": set(),
        "jtag": set(),
    }
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT fault_site_key, fault_type, status, exclusion,
                   hold_unresolved, blackbox_unresolved
            FROM faults WHERE campaign_id = ? AND collapsed_into IS NULL
            """,
            (campaign_id,),
        ).fetchall()
        data = summary(conn, campaign_id=campaign_id)
    for row in rows:
        site = (str(row["fault_site_key"]), str(row["fault_type"]).lower())
        if row["exclusion"] == "jtag":
            classes["jtag"].add(site)
        if int(row["hold_unresolved"]):
            assert row["status"] == "undetected", site
            classes["hold"].add(site)
        if int(row["blackbox_unresolved"]):
            assert row["status"] == "undetected", site
            classes["blackbox"].add(site)
        if row["status"] == "redundant":
            classes["redundant"].add(site)
    return classes, data, stats


def _both(site: str) -> set[tuple[str, str]]:
    return {(site, "sa0"), (site, "sa1")}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("launch_mode", "expected_hold", "expected_redundant"),
    [
        (
            None,
            # Through Y only, and Y needs Q = 1. Y s-a-1 is seen with Q = 0.
            _both("net:5:branch:g_and:B")
            | _both("net:5:stem")
            | {("net:7:stem", "sa0")},
            # Z = A | 1: nothing reaches Z, whatever Q is.
            _both("net:5:branch:g_or:A")
            | {("net:12:stem", "sa1"), ("net:-2:stem", "sa1")},
        ),
        (
            "loc",
            # Y transitions only if Q does. Free in the hold twin, Q is not held
            # between the launch and capture frames like a real input: held, it
            # could never transition and Y's faults would be misfiled redundant.
            _both("net:7:stem"),
            # A and D are real inputs, held launch -> capture, and Z and the
            # constant are constant: none of them ever transitions.
            _both("net:5:stem")
            | _both("net:5:branch:g_and:B")
            | _both("net:5:branch:g_or:A")
            | _both("net:12:stem")
            | _both("net:-2:stem")
            | _both("net:3:stem"),
        ),
    ],
)
def test_unsat_only_because_of_a_hold_is_hold_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
    launch_mode: str | None,
    expected_hold: set[tuple[str, str]],
    expected_redundant: set[tuple[str, str]],
) -> None:
    monkeypatch.chdir(tmp_path)

    classes, data, stats = _run(tmp_path, with_memory=False, launch_mode=launch_mode)

    assert classes["hold"] == expected_hold
    assert classes["redundant"] == expected_redundant
    assert classes["blackbox"] == set()
    assert data["hold_unresolved"] == len(expected_hold)
    # The TAP flop's own output, and TDI, which only it reads, are JTAG's.
    assert _both("net:11:stem") | _both("net:10:stem") <= classes["jtag"]
    # Classified once, then out of the active set: nothing is left to retry.
    assert stats.terminal_reason == "COMPLETE"
    assert stats.rounds == 1


@pytest.mark.unit
def test_what_needs_the_holds_and_a_blackbox_is_blackbox_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """The hold twin keeps the memory opaque, so a fault W alone sees -- W needs
    dout and Q both -- is UNSAT there too. The blackbox twin sets both free: a
    test exists, so it is blackbox_unresolved, not redundant. A fault Y sees still
    needs the holds lifted only: hold_unresolved."""
    monkeypatch.chdir(tmp_path)

    classes, data, stats = _run(tmp_path, with_memory=True, launch_mode=None)

    assert classes["hold"] == (
        _both("net:5:branch:g_and:B")
        | _both("net:5:stem")
        | {("net:7:stem", "sa0"), ("net:11:branch:g_and:A", "sa0")}
    )
    assert classes["blackbox"] == (
        _both("net:14:stem")
        | _both("net:13:stem")
        | _both("net:11:branch:g_both:B")
        | _both("net:15:stem")
    )
    assert classes["redundant"] == (
        _both("net:5:branch:g_or:A") | {("net:12:stem", "sa1"), ("net:-2:stem", "sa1")}
    )
    assert data["hold_unresolved"] == len(classes["hold"])
    assert data["blackbox_unresolved"] == len(classes["blackbox"])
    assert stats.terminal_reason == "COMPLETE"


@pytest.mark.unit
def test_the_hold_twin_frees_the_nonscan_ties_and_observes_their_pins() -> None:
    """Only ties and ports change: each tie becomes a buffer from a free input,
    each pin reader drives an output, and no X source is left."""
    module = _mini(with_memory=False)
    module["cells"] = {
        "$nstie0_u_tap__st": {
            "type": "$faultflow_tie0",
            "attributes": {"faultflow_nonscan": "u_tap__st"},
            "port_directions": {"Y": "output"},
            "connections": {"Y": [11]},
        },
        "$nsx_u_tap__free": {
            "type": "$faultflow_tie0",
            "attributes": {
                "faultflow_nonscan": "u_tap__free",
                "faultflow_nonscan_x": "1",
            },
            "port_directions": {"Y": "output"},
            "connections": {"Y": [16]},
        },
        "$nshold_TRST_N": {
            "type": "$faultflow_tie0",
            "attributes": {},
            "port_directions": {"Y": "output"},
            "connections": {"Y": [6]},
        },
        "$nssink_u_tap__st_D": {
            "type": "$faultflow_observe_buf",
            "attributes": {"faultflow_nonscan": "u_tap__st"},
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [10], "Y": [17]},
        },
    }
    view = {"modules": {TOP: module}}
    assert x_source_nets(module, ()) == [16]

    assert make_nonscan_free(view, TOP)

    ports, cells = module["ports"], module["cells"]
    free = {
        name: port["bits"][0]
        for name, port in ports.items()
        if name.startswith(NONSCAN_FREE_PORT_PREFIX)
    }
    assert set(free) == {
        "__nsfree_nstie0_u_tap__st",
        "__nsfree_nsx_u_tap__free",
        "__nsfree_nshold_TRST_N",
    }
    assert cells["$nstie0_u_tap__st"]["connections"] == {
        "A": [free["__nsfree_nstie0_u_tap__st"]],
        "Y": [11],
    }
    assert ports[NONSCAN_OBSERVE_PORT_PREFIX + "u_tap__st_D"] == {
        "direction": "output",
        "bits": [17],
    }
    assert x_source_nets(module, ()) == []
    assert not make_nonscan_free({"modules": {TOP: _mini(with_memory=False)}}, TOP)
