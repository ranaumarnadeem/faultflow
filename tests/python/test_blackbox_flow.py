"""Phase 9 — instance blackboxing end-to-end (boundary observation + reporting).

Fixture tiny_bb_boundary: a(2),b(3),c(4) PIs; y(7) PO
  g_up: and2_1 -> up(5) ; u_bb: inv_1 -> bbout(6) ; g_dn: xor2_1 -> y(7)
Blackboxing u_bb makes up(5) an observable pseudo-PO and bbout(6) a controllable
pseudo-PI; the instance itself is not simulated.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.db import connect, init_schema
from faultflow.reporter.coverage import _policy, write_reports
from db_v3_helpers import insert_campaign

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_blackbox_boundary.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "tiny_bb_boundary"


def _exhaustive_abc() -> list[dict[str, bool]]:
    return [{"a": bool(m & 1), "b": bool(m & 2), "c": bool(m & 4)} for m in range(8)]


# --------------------------------------------------------------------------- #
# 9-I (reporter policy block) — unit
# --------------------------------------------------------------------------- #
def test_policy_includes_blackbox_fields() -> None:
    pol = _policy({"unsupported_cells": "fail"}, ("u_bb", "u_pll"))
    assert pol["blackbox_instances"] == ["u_bb", "u_pll"]
    assert pol["blackbox_boundary"] == "pseudo_port"


def test_policy_no_blackbox_is_none() -> None:
    pol = _policy({})
    assert pol["blackbox_instances"] == []
    assert pol["blackbox_boundary"] == "none"


# --------------------------------------------------------------------------- #
# 9-I01/I02 — simulate_to_db on the blackbox graph + coverage report
# --------------------------------------------------------------------------- #
def _write_cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {FIXTURE}
cell_lib = {CELL_MAP}

[fault_model]
model = stuck_at

[blackbox]
instances = u_bb
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg_path


@pytest.mark.golden
def test_blackbox_simulate_and_report(tmp_path: Path, require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    cfg = load_config(_write_cfg(tmp_path), TOP)
    cfg = replace(cfg, output_root=tmp_path / "output")
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    summary = core.simulate_to_db(
        str(FIXTURE),
        str(CELL_MAP),
        str(cfg.db_path),
        campaign_id,
        _exhaustive_abc(),
        ["a", "b", "c"],
        "test",
        blackbox_instances=["u_bb"],
    )
    # Instance blackbox does NOT create excluded_blackbox faults (that is the
    # unknown-cell path); the boundary nets are ordinary testable faults.
    assert summary["denominator"] > 0
    assert summary["excluded_blackbox"] == 0

    conn = connect(cfg.db_path)
    init_schema(conn)
    # 9-I02: boundary observation — the blackbox input net (up) feeds only the
    # un-elaborated blackbox, so it is detectable ONLY via its pseudo-PO TP.
    up_rows = conn.execute(
        "SELECT status FROM faults WHERE campaign_id = ? AND net_name = 'up'",
        (campaign_id,),
    ).fetchall()
    assert up_rows, "expected fault rows on the blackbox input net 'up'"
    assert any(str(r["status"]) == "detected" for r in up_rows)

    # 9-I01: full report carries the blackbox policy; write_reports also enforces
    # the Policy-3 invariant (it raises on mismatch, so reaching here proves it).
    _json_path, _txt_path, report = write_reports(conn, cfg, campaign_id=campaign_id)
    conn.close()
    assert report["policy"]["blackbox_instances"] == ["u_bb"]
    assert report["policy"]["blackbox_boundary"] == "pseudo_port"


# --------------------------------------------------------------------------- #
# 9-II: blackbox_instances threading through native SAT ATPG path
# --------------------------------------------------------------------------- #


def _make_cfg(tmp_path: Path) -> "FaultflowConfig":  # type: ignore[name-defined]
    from dataclasses import replace

    cfg = load_config(_write_cfg(tmp_path), TOP)
    return replace(cfg, output_root=tmp_path / "output")


@pytest.mark.golden
def test_ensure_faults_enumerated_with_blackbox(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """ensure_faults_enumerated must accept blackbox_instances and use the
    blackbox graph (pseudo-PI bbout + TP up instead of elaborating u_bb)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    cfg = _make_cfg(tmp_path)
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    core.ensure_faults_enumerated(
        str(FIXTURE),
        str(CELL_MAP),
        str(cfg.db_path),
        campaign_id,
        blackbox_instances=["u_bb"],
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    rows = conn.execute(
        "SELECT net_name, status FROM faults WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    conn.close()

    net_names = {r["net_name"] for r in rows}
    # Boundary nets must have fault rows with the blackbox graph.
    assert "up" in net_names, f"TP net 'up' missing from faults; got {net_names}"
    assert "bbout" in net_names, f"pseudo-PI 'bbout' missing from faults; got {net_names}"
    # All rows start as undetected
    assert all(r["status"] == "undetected" for r in rows)


@pytest.mark.golden
def test_solve_fault_atpg_with_blackbox(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """solve_fault_atpg must use the blackbox graph so that boundary-net fault
    IDs (compiled_net_index) stay consistent between enumeration and solving."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    cfg = _make_cfg(tmp_path)
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    core.ensure_faults_enumerated(
        str(FIXTURE),
        str(CELL_MAP),
        str(cfg.db_path),
        campaign_id,
        blackbox_instances=["u_bb"],
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    # Pick the first active fault on the boundary pseudo-PI 'bbout'.
    row = conn.execute(
        """SELECT id FROM faults
           WHERE campaign_id = ? AND net_name = 'bbout'
             AND status = 'undetected' AND exclusion = 'none'
             AND collapsed_into IS NULL
           LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    conn.close()

    assert row is not None, "no active faults on 'bbout' after blackbox enumeration"
    fault_id = int(row["id"])

    result = dict(
        core.solve_fault_atpg(
            str(FIXTURE),
            str(CELL_MAP),
            str(cfg.db_path),
            fault_id,
            [],
            blackbox_instances=["u_bb"],
        )
    )
    # bbout is a free PI in the blackbox graph — a SA fault on it must be SAT.
    assert result["result"] == "SAT", (
        f"expected SAT for bbout boundary fault, got {result['result']}"
    )


@pytest.mark.golden
def test_native_atpg_full_flow_with_blackbox(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """run_progressive_native_atpg must honour cfg.blackbox_instances and
    produce a coverage report whose policy carries blackbox_instances."""
    from faultflow.db import summary
    from faultflow.reporter.coverage import write_reports
    from faultflow.runner.progressive_atpg import (
        redundancy_model_id,
        run_progressive_native_atpg,
    )

    cfg = _make_cfg(tmp_path)
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    # Build a minimal fingerprint dict so redundancy_model_id can be computed.
    fp_dict = {
        "netlist_hash": "test",
        "cell_lib_hash": "test",
        "collapsing": False,
        "unsupported_cells": cfg.simulation.unsupported_cells,
        "include_clock_faults": cfg.fault_model.include_clock_faults,
        "include_reset_faults": cfg.fault_model.include_reset_faults,
        "fault_model": "stuck_at",
    }
    red_model = redundancy_model_id(fp_dict)

    _, stats, _run_id, _atpg_s, _sim_s = run_progressive_native_atpg(
        cfg,
        FIXTURE,
        red_model,
        campaign_id=campaign_id,
        max_rounds=5,
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    data = summary(conn, campaign_id=campaign_id)
    _, _txt, report = write_reports(conn, cfg, campaign_id=campaign_id)
    conn.close()

    assert data["denominator"] > 0, "denominator must be > 0 after blackbox ATPG"
    cov = data.get("coverage_percent") or 0.0
    assert cov > 0.0, f"coverage must be > 0 after blackbox ATPG, got {cov}"
    assert report["policy"]["blackbox_instances"] == ["u_bb"]
    assert report["policy"]["blackbox_boundary"] == "pseudo_port"


@pytest.mark.golden
def test_redundant_fault_consistent_with_blackbox(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A fault classified UNSAT-redundant under the blackbox graph must remain
    redundant when invalidate_stale_redundant is called with the same model_id
    (i.e. no spurious re-activation on the same configuration)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.runner.progressive_atpg import redundancy_model_id

    cfg = _make_cfg(tmp_path)
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    core.ensure_faults_enumerated(
        str(FIXTURE),
        str(CELL_MAP),
        str(cfg.db_path),
        campaign_id,
        blackbox_instances=["u_bb"],
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    row = conn.execute(
        """SELECT id FROM faults
           WHERE campaign_id = ? AND status = 'undetected'
             AND exclusion = 'none' AND collapsed_into IS NULL
           ORDER BY id LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    fault_id = int(row["id"])

    red_model = redundancy_model_id(
        {
            "netlist_hash": "test",
            "cell_lib_hash": "test",
            "collapsing": False,
            "unsupported_cells": cfg.simulation.unsupported_cells,
            "include_clock_faults": cfg.fault_model.include_clock_faults,
            "include_reset_faults": cfg.fault_model.include_reset_faults,
            "fault_model": "stuck_at",
        }
    )
    core.mark_fault_redundant(str(cfg.db_path), fault_id, red_model)

    # Same model_id -> must NOT re-activate the redundant fault.
    core.invalidate_stale_redundant(str(cfg.db_path), campaign_id, red_model)

    conn = connect(cfg.db_path)
    init_schema(conn)
    status = conn.execute(
        "SELECT status FROM faults WHERE id = ?", (fault_id,)
    ).fetchone()
    conn.close()

    assert status is not None
    assert status["status"] == "redundant", (
        "fault must stay redundant after invalidate_stale_redundant with same model_id"
    )


# --------------------------------------------------------------------------- #
# 9-III: redundancy_model_id must differ when blackbox_instances changes
# --------------------------------------------------------------------------- #


def test_redundancy_model_id_differs_by_blackbox(tmp_path: Path) -> None:
    """redundancy_model_id must produce different strings for different
    blackbox_instances so that stale UNSAT-redundant classifications are
    invalidated when the blackbox config changes."""
    from faultflow.runner.progressive_atpg import redundancy_model_id

    base_fp = {
        "netlist_hash": "abc",
        "cell_lib_hash": "def",
        "collapsing": False,
        "unsupported_cells": "fail",
        "include_clock_faults": False,
        "include_reset_faults": False,
        "fault_model": "stuck_at",
        "launch": "loc",
    }

    no_bb = redundancy_model_id({**base_fp, "blackbox_instances": []})
    with_bb = redundancy_model_id({**base_fp, "blackbox_instances": ["u_bb"]})
    diff_bb = redundancy_model_id({**base_fp, "blackbox_instances": ["u_pll"]})
    # Order-independent: sorted internally.
    both_bb_a = redundancy_model_id(
        {**base_fp, "blackbox_instances": ["u_bb", "u_pll"]}
    )
    both_bb_b = redundancy_model_id(
        {**base_fp, "blackbox_instances": ["u_pll", "u_bb"]}
    )

    assert no_bb != with_bb, "no-blackbox model must differ from blackboxed model"
    assert with_bb != diff_bb, "different instance names must give different model IDs"
    assert both_bb_a == both_bb_b, "instance order must not matter (sorted internally)"
    assert no_bb != both_bb_a


@pytest.mark.golden
def test_stale_redundant_invalidated_on_blackbox_change(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A fault marked redundant under the blackbox model (with u_bb) must be
    re-activated when invalidate_stale_redundant is called with a DIFFERENT
    model_id (no blackbox), because the graph changes."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.runner.progressive_atpg import redundancy_model_id

    cfg = _make_cfg(tmp_path)
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    # Enumerate faults with the blackbox graph.
    core.ensure_faults_enumerated(
        str(FIXTURE),
        str(CELL_MAP),
        str(cfg.db_path),
        campaign_id,
        blackbox_instances=["u_bb"],
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    row = conn.execute(
        """SELECT id FROM faults
           WHERE campaign_id = ? AND status = 'undetected'
             AND exclusion = 'none' AND collapsed_into IS NULL
           ORDER BY id LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    fault_id = int(row["id"])

    base_fp = {
        "netlist_hash": "test",
        "cell_lib_hash": "test",
        "collapsing": False,
        "unsupported_cells": cfg.simulation.unsupported_cells,
        "include_clock_faults": cfg.fault_model.include_clock_faults,
        "include_reset_faults": cfg.fault_model.include_reset_faults,
        "fault_model": "stuck_at",
    }
    bb_model = redundancy_model_id({**base_fp, "blackbox_instances": ["u_bb"]})
    no_bb_model = redundancy_model_id({**base_fp, "blackbox_instances": []})

    assert bb_model != no_bb_model, "pre-condition: model IDs must differ"

    # Mark redundant under the blackbox model.
    core.mark_fault_redundant(str(cfg.db_path), fault_id, bb_model)

    # Switching to the no-blackbox model must RE-ACTIVATE the fault.
    core.invalidate_stale_redundant(str(cfg.db_path), campaign_id, no_bb_model)

    conn = connect(cfg.db_path)
    init_schema(conn)
    status = conn.execute(
        "SELECT status FROM faults WHERE id = ?", (fault_id,)
    ).fetchone()
    conn.close()

    assert status is not None
    assert status["status"] == "undetected", (
        "fault must be re-activated (undetected) when the blackbox model changes"
    )


# --------------------------------------------------------------------------- #
# 9-IV: transition ATPG + blackbox end-to-end
# --------------------------------------------------------------------------- #


def _write_transition_bb_cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {FIXTURE}
cell_lib = {CELL_MAP}

[fault_model]
model = transition

[blackbox]
instances = u_bb
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg_path


@pytest.mark.golden
def test_transition_atpg_with_blackbox(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """run_progressive_transition_atpg on the blackbox fixture must produce
    coverage > 0 and carry policy.blackbox_instances in the report."""
    from dataclasses import replace

    from faultflow.db import summary
    from faultflow.reporter.coverage import write_reports
    from faultflow.runner.progressive_atpg import (
        redundancy_model_id,
        run_progressive_transition_atpg,
    )

    cfg = load_config(_write_transition_bb_cfg(tmp_path), TOP)
    cfg = replace(cfg, output_root=tmp_path / "output")
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    fp_dict = {
        "netlist_hash": "test",
        "cell_lib_hash": "test",
        "collapsing": False,
        "unsupported_cells": cfg.simulation.unsupported_cells,
        "include_clock_faults": cfg.fault_model.include_clock_faults,
        "include_reset_faults": cfg.fault_model.include_reset_faults,
        "fault_model": "transition",
        "blackbox_instances": list(cfg.blackbox_instances),
    }
    red_model = redundancy_model_id(fp_dict)

    _, stats, _run_id, _atpg_s, _sim_s = run_progressive_transition_atpg(
        cfg,
        FIXTURE,
        red_model,
        campaign_id=campaign_id,
        max_rounds=5,
    )

    conn = connect(cfg.db_path)
    init_schema(conn)
    data = summary(conn, campaign_id=campaign_id)
    _, _txt, report = write_reports(conn, cfg, campaign_id=campaign_id)
    conn.close()

    assert data["denominator"] > 0
    cov = data.get("coverage_percent") or 0.0
    assert cov > 0.0, f"transition ATPG + blackbox must detect ≥1 fault, got {cov}%"
    assert report["policy"]["blackbox_instances"] == ["u_bb"]
    assert report["policy"]["blackbox_boundary"] == "pseudo_port"
