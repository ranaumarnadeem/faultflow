"""Stage 6 of the X engine: a scan campaign on it ([simulation] engine = x)
simulates the unknown values the two-valued flow masks by cone (scan.x_mask) -- a
blackbox output, a frozen non-scan flop -- and powers every flop up unknown in
the scan protocol.

The design: u0 scans; u_tap__st, a non-scan TAP flop, is held in reset, so its Q
is 0. Y = Q & A; W = dout & Q, u_mem's unknown output blocked by the held Q; P =
dout | B, blocked when B = 1. The cone mask hides W and P in every pattern; the X
engine sees W = 0 always and P = 1 whenever B = 1."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from faultflow.db import connect, init_schema, summary
from faultflow.db.campaign import ensure_campaign
from faultflow.runner.progressive_atpg import redundancy_model_id
from faultflow.scan.atpg_view import UNKNOWN_ATTR, build_scan_atpg_view
from faultflow.scan.detection_pipeline import (
    build_scan_pipeline_context,
    run_progressive_scan_atpg,
)
from faultflow.scan.errors import ScanError

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "xs"


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


def _design(
    with_memory: bool, capture_memory: bool = False, with_xbit: bool = False
) -> dict[str, Any]:
    """`capture_memory`: also u1, a scan flop capturing dout & A -- unknown in the
    patterns with A = 1. `with_xbit`: also V = A | x, an x bit of the netlist."""
    ports = {"CLK": 2, "D": 3, "A": 5, "TRST_N": 6, "TCK": 9, "TDI": 10}
    outputs = {"Q": 4, "Y": 7}
    cells = {
        "u0": _cell("sky130_fd_sc_hd__dfxtp_1", CLK=[2], D=[3], Q=[4]),
        "u_tap__st": _cell(
            "sky130_fd_sc_hd__dfrtp_1", CLK=[9], D=[10], RESET_B=[6], Q=[11]
        ),
        "g_and": _cell("sky130_fd_sc_hd__and2_1", A=[11], B=[5], X=[7]),
    }
    if with_xbit:
        outputs["V"] = 30
        cells["g_x"] = _cell("sky130_fd_sc_hd__or2_1", A=[5], B=["x"], X=[30])
    if capture_memory:
        outputs["Q1"] = 19
        cells["g_cap"] = _cell("sky130_fd_sc_hd__and2_1", A=[13], B=[5], X=[18])
        cells["u1"] = _cell("sky130_fd_sc_hd__dfxtp_1", CLK=[2], D=[18], Q=[19])
    if with_memory:
        ports.update({"MDIN": 15, "B": 16})
        outputs.update({"W": 14, "P": 17})
        cells["u_mem"] = _cell("sram_like", din=[15], dout=[13])
        cells["g_both"] = _cell("sky130_fd_sc_hd__and2_1", A=[13], B=[11], X=[14])
        cells["g_p"] = _cell("sky130_fd_sc_hd__or2_1", A=[13], B=[16], X=[17])
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
    tmp_path: Path,
    *,
    engine: str,
    with_memory: bool,
    capture_memory: bool = False,
    random_vectors: int = 0,
) -> tuple[Any, Path, Any, dict[str, object]]:
    from faultflow.config import load_config
    from faultflow.runner import Runner
    from faultflow.runner.runner import _load_core, _port_names
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp
    from faultflow.scan.x_mask import mask_scan_view

    source = tmp_path / "xs.json"
    source.write_text(
        json.dumps({"modules": {TOP: _design(with_memory, capture_memory)}}),
        encoding="utf-8",
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
engine = {engine}
[atpg]
mode = comb
random_vectors = {random_vectors}
max_rounds = 4
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
    x_mask = mask_scan_view(
        _load_core(),
        cfg,
        view,
        atpg_view,
        manifest,
        generic,
        functional_outputs,
        launch_mode=None,
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
        "config_hash": f"cfg-{engine}",
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
    if engine != "two_valued":
        fp["engine"] = engine
    return cfg, atpg_view, scan_ctx, fp


def _run(
    tmp_path: Path,
    *,
    engine: str,
    with_memory: bool,
    capture_memory: bool = False,
    random_vectors: int = 0,
) -> tuple[dict[tuple[str, str], str], dict[str, Any], list[dict[str, Any]]]:
    """Each fault's class after the campaign, the summary, and the exported
    patterns."""
    work = tmp_path / engine
    work.mkdir()
    cwd = os.getcwd()
    os.chdir(work)  # the campaign's output directory is relative
    try:
        cfg, atpg_view, scan_ctx, fp = _workspace(
            work,
            engine=engine,
            with_memory=with_memory,
            capture_memory=capture_memory,
            random_vectors=random_vectors,
        )
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            campaign_id = ensure_campaign(conn, "scan", fp)
        patterns = work / "patterns.json"
        run_progressive_scan_atpg(
            cfg,
            atpg_view,
            redundancy_model_id(fp),
            campaign_id=campaign_id,
            scan_ctx=scan_ctx,
            max_rounds=4,
            target_coverage=100.0,
            scan_pattern_out=patterns,
        )
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
            data = summary(
                conn,
                campaign_id=campaign_id,
                possible_credit=cfg.fault_model.possible_credit,
            )
    finally:
        os.chdir(cwd)
    classes: dict[tuple[str, str], str] = {}
    for row in rows:
        site = (str(row["fault_site_key"]), str(row["fault_type"]).lower())
        tag = str(row["status"])
        if row["exclusion"] != "none":
            tag = "excluded_" + str(row["exclusion"])
        elif int(row["blackbox_unresolved"]):
            tag = "blackbox_unresolved"
        elif int(row["hold_unresolved"]):
            tag = "hold_unresolved"
        classes[site] = tag
    exported = (
        json.loads(patterns.read_text(encoding="utf-8")) if patterns.exists() else []
    )
    return classes, data, exported


@pytest.mark.unit
def test_with_nothing_unknown_the_x_engine_grades_as_the_two_valued(
    tmp_path: Path,
) -> None:
    two, two_data, _ = _run(tmp_path, engine="two_valued", with_memory=False)
    x, x_data, _ = _run(tmp_path, engine="x", with_memory=False)
    assert x == two
    assert x_data["possible"] == 0
    assert x_data["coverage_percent"] == two_data["coverage_percent"]


@pytest.mark.unit
def test_the_x_engine_credits_what_the_cone_mask_hid(tmp_path: Path) -> None:
    two, two_data, _ = _run(tmp_path, engine="two_valued", with_memory=True)
    x, x_data, exported = _run(tmp_path, engine="x", with_memory=True)
    differs = {site: (two[site], x[site]) for site in two if two[site] != x[site]}
    W, P, B = 14, 17, 16
    assert differs == {
        # W = dout & Q, Q held 0: W is 0 whatever the memory holds.
        (f"net:{W}:stem", "sa1"): ("blackbox_unresolved", "detected"),
        # P = dout | B: 1 whenever B = 1.
        (f"net:{P}:stem", "sa0"): ("blackbox_unresolved", "detected"),
        # B s-a-0 with B = 1: P good 1, faulty dout -- unknown.
        (f"net:{B}:stem", "sa0"): ("blackbox_unresolved", "possible"),
    }
    assert x_data["possible"] == 1
    assert x_data["detected"] == two_data["detected"] + 2
    # A possible detection counts half ([fault_model] possible_credit = 50).
    credited = x_data["detected"] + 0.5 * x_data["possible"]
    assert x_data["coverage_percent"] == pytest.approx(
        100.0 * credited / x_data["denominator"]
    )
    # Each exported pattern compares P only where it is known: B = 1.
    assert exported
    for pattern in exported:
        values = pattern["capture_pi_values"]
        assert ("P" in values) == bool(values["B"])
        assert values.get("W") is False


@pytest.mark.unit
def test_the_x_engine_view_marks_its_unknown_ties_and_masks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.runner.runner import _load_core
    from faultflow.scan.atpg_view import UNOBSERVED_NETS_ATTR
    from faultflow.scan.stitch import _top_module

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, _ = _workspace(tmp_path, engine="x", with_memory=True)
    view = json.loads(atpg_view.read_text(encoding="utf-8"))
    _, module = _top_module(view, TOP)
    marked = [
        name
        for name, cell in module["cells"].items()
        if cell.get("attributes", {}).get(UNKNOWN_ATTR)
    ]
    assert marked and all(name.startswith("$bbtie0_u_mem") for name in marked)
    assert UNOBSERVED_NETS_ATTR not in module.get("attributes", {})
    assert scan_ctx.x_mask.x_engine and not scan_ctx.x_mask.nets
    # A two-valued load of the view refuses its unknown ties.
    core = _load_core()
    with pytest.raises(Exception, match="faultflow_unknown"):
        core.list_site_keys(str(atpg_view), str(CELL_MAP), "fail")
    assert core.list_site_keys(str(atpg_view), str(CELL_MAP), "fail+x")


@pytest.mark.unit
def test_an_x_campaign_refuses_a_view_with_an_unmarked_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.scan.detection_pipeline import _require_x_mask
    from faultflow.scan.stitch import _top_module

    monkeypatch.chdir(tmp_path)
    cfg, atpg_view, scan_ctx, _ = _workspace(tmp_path, engine="x", with_memory=True)
    _require_x_mask(scan_ctx, str(atpg_view), None)
    view = json.loads(atpg_view.read_text(encoding="utf-8"))
    _, module = _top_module(view, TOP)
    for cell in module["cells"].values():
        cell.get("attributes", {}).pop(UNKNOWN_ATTR, None)
    atpg_view.write_text(json.dumps(view), encoding="utf-8")
    with pytest.raises(ScanError, match="mark every tie"):
        _require_x_mask(scan_ctx, str(atpg_view), None)


@pytest.mark.unit
def test_a_flop_capturing_an_unknown_keeps_its_q_stem_faults(tmp_path: Path) -> None:
    """u1 captures dout & A, unknown when A = 1. Its stuck Q still corrupts the
    bits shifted out through it, which the X engine's scan protocol grades (every
    flop powered up unknown, dout unknown in every cycle). Every fault the cone
    mask let the two-valued flow detect, the X engine detects too: outside an
    unknown's reach its values are known, and its SAT finds such a test. Random
    patterns load A = 1 too, the ones that leave u1's unload bit don't-care."""
    two, _, _ = _run(
        tmp_path,
        engine="two_valued",
        with_memory=True,
        capture_memory=True,
        random_vectors=16,
    )
    x, _, exported = _run(
        tmp_path, engine="x", with_memory=True, capture_memory=True, random_vectors=16
    )
    detected_two = {site for site, tag in two.items() if tag == "detected"}
    detected_x = {site for site, tag in x.items() if tag == "detected"}
    assert detected_two <= detected_x
    q1 = [site for site in x if site[0].startswith("net:19:")]
    assert q1 and all(x[site] == "detected" for site in q1)
    # Per pattern: u1's bit is don't-care exactly where it captured dout.
    masked = [p for p in exported if p.get("unload_mask")]
    assert masked and all(p["capture_pi_values"]["A"] for p in masked)
    assert any(not p["capture_pi_values"]["A"] for p in exported)


@pytest.mark.unit
def test_an_x_campaigns_fault_indices_are_its_x_mode_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An x bit is its own unknown net in an X-mode graph, a constant 0 in a
    two-valued one, so their compiled indices differ. The faults of a scan
    campaign on the X engine carry the X-mode graph's, so a tool grading them
    later by index (ff.py jtag) must load the scanned netlist with the
    campaign's graph policy (SimulationConfig.graph_policy)."""
    from faultflow.runner.runner import _load_core

    original = _design
    monkeypatch.setattr(
        sys.modules[__name__],
        "_design",
        lambda m, c=False: original(m, c, with_xbit=True),
    )
    work = tmp_path / "x"
    work.mkdir()
    monkeypatch.chdir(work)
    cfg, atpg_view, scan_ctx, fp = _workspace(work, engine="x", with_memory=False)
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = ensure_campaign(conn, "scan", fp)
    run_progressive_scan_atpg(
        cfg,
        atpg_view,
        redundancy_model_id(fp),
        campaign_id=campaign_id,
        scan_ctx=scan_ctx,
        max_rounds=2,
        target_coverage=100.0,
    )
    with connect(cfg.db_path) as conn:
        stored = {
            str(row["fault_site_key"]): int(row["compiled_net_index"])
            for row in conn.execute(
                "SELECT fault_site_key, compiled_net_index FROM faults "
                "WHERE campaign_id = ?",
                (campaign_id,),
            )
        }
    core = _load_core()

    def indices(policy: str) -> dict[str, int]:
        return {
            str(row["site_key"]): int(row["compiled_net_index"])
            for row in core.list_site_keys(
                str(scan_ctx.generic_json), str(CELL_MAP), policy, []
            )
        }

    assert cfg.simulation.graph_policy == "fail+x"
    x_mode = indices(cfg.simulation.graph_policy)
    assert all(x_mode[key] == index for key, index in stored.items())
    two = indices("fail")
    assert any(two.get(key) != index for key, index in stored.items())
