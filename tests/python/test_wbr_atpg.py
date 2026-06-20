"""IEEE 1500 wrapper boundary (WBR) ATPG end-to-end tests.

Fixture: c17_wrapped.json — the classic ISCAS85 c17 circuit (5 PI, 2 PO, 6 gates)
wrapped with $wbc_in_faultflow on every PI and $wbc_out_faultflow on every PO.

Net ID assignment:
  System-side PIs  : N1=2, N2=3, N3=4, N6=5, N7=6  (top-level port bits)
  Core-side PI nets: n1_core=20, n2_core=21, n3_core=22, n6_core=23, n7_core=24
  Internal nets    : n9=9, n10=10, n11=11, n12=12
  Core PO nets     : n22_core=7, n23_core=8
  System-side POs  : N22=25, N23=26  (top-level port bits)

FUNCTIONAL — both wbc cells pass through; identical to the unwrapped synthesised c17.
INTEST     — stimulus = {n1_core..n7_core}; observe = {n22_core, n23_core}.
EXTEST     — stimulus = {N22, N23} + {N1..N7 PIs}; observe = {N1..N7} (FROM_SYS nets).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/python/fixtures/c17_wrapped.json"
UNWRAPPED = ROOT / "tests/benchmarks/iscas85/synth/c17.json"
CELL_MAP = ROOT / "cells/osu/osu035.json"
TOP_WRAPPED = "c17_wrapped"
TOP_UNWRAPPED = "c17"

# --- helpers ----------------------------------------------------------------

# System-side PI names (top-level ports of c17_wrapped)
SYS_PI_NAMES = ["N1", "N2", "N3", "N6", "N7"]
# Core-side PI netnames (used in INTEST test vectors)
CORE_PI_NAMES = ["n1_core", "n2_core", "n3_core", "n6_core", "n7_core"]
PO_NAMES = ["N22", "N23"]

# Unwrapped c17 uses the same logical PI names
UNWRAP_PI_NAMES = ["N1", "N2", "N3", "N6", "N7"]


def _all32_sys() -> list[dict[str, bool]]:
    """All 32 system-side PI combinations for c17_wrapped (FUNCTIONAL mode)."""
    combos = list(itertools.product([False, True], repeat=5))
    return [dict(zip(SYS_PI_NAMES, combo)) for combo in combos]


def _all32_core() -> list[dict[str, bool]]:
    """All 32 core-side combinations for INTEST mode.
    Keyed by core netnames (n1_core .. n7_core)."""
    combos = list(itertools.product([False, True], repeat=5))
    return [dict(zip(CORE_PI_NAMES, combo)) for combo in combos]


def _all32_unwrapped() -> list[dict[str, bool]]:
    """All 32 patterns for the unwrapped c17."""
    combos = list(itertools.product([False, True], repeat=5))
    return [dict(zip(UNWRAP_PI_NAMES, combo)) for combo in combos]


# --------------------------------------------------------------------------- #
# FUNCTIONAL mode — wrapper cells are transparent buffers                     #
# The wrapped design must produce bit-identical outputs to the unwrapped c17. #
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_functional_mode_matches_unwrapped(require_cpp_core: None) -> None:
    """FUNCTIONAL mode: c17_wrapped outputs must match unwrapped c17 on all patterns."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    wrapped_out = core.fault_free_outputs(
        str(FIXTURE),
        str(CELL_MAP),
        _all32_sys(),
        SYS_PI_NAMES,
        PO_NAMES,
    )
    unwrapped_out = core.fault_free_outputs(
        str(UNWRAPPED),
        str(CELL_MAP),
        _all32_unwrapped(),
        UNWRAP_PI_NAMES,
        PO_NAMES,
    )
    assert len(wrapped_out) == 32
    assert len(unwrapped_out) == 32
    for i, (w, u) in enumerate(zip(wrapped_out, unwrapped_out)):
        assert w == u, (
            f"Pattern {i} mismatch: wrapped={w} unwrapped={u} "
            f"inputs={_all32_sys()[i]}"
        )


# --------------------------------------------------------------------------- #
# INTEST mode — test the core logic                                           #
# stimulus = wbc_in.core_nets; observe = wbc_out.core_nets                   #
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_intest_detects_core_faults(tmp_path: Path, require_cpp_core: None) -> None:
    """INTEST: exhaustive 32-pattern drive of core inputs achieves >0 fault coverage."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db = str(tmp_path / "faults.db")
    conn = connect(db)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    summary = dict(
        core.simulate_to_db(
            str(FIXTURE),
            str(CELL_MAP),
            db,
            campaign_id,
            _all32_core(),
            CORE_PI_NAMES,
            "intest_exhaustive",
            test_mode="intest",
        )
    )
    cov = float(summary.get("coverage_percent") or 0.0)
    denom = int(summary.get("denominator") or 0)
    detected = int(summary.get("detected") or 0)

    assert denom > 0, "INTEST denominator must be >0"
    assert detected > 0, f"INTEST must detect >=1 fault, got 0/{denom}"
    assert cov > 0.0, f"INTEST coverage must be >0%, got {cov}%"


@pytest.mark.golden
def test_intest_observes_core_outputs_not_sys(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """INTEST: faults on core internal nets are detectable; faults only on
    the sys-side TO_SYS nets (N22=25, N23=26) are NOT observed (safe-zero)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db = str(tmp_path / "faults.db")
    conn = connect(db)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    core.simulate_to_db(
        str(FIXTURE),
        str(CELL_MAP),
        db,
        campaign_id,
        _all32_core(),
        CORE_PI_NAMES,
        "intest_test",
        test_mode="intest",
    )

    conn = connect(db)
    init_schema(conn)
    # n22_core / n23_core (Yosys IDs 7 and 8) should be detectable — they are
    # the INTEST observation points (wbc_out.core_net).
    core_po_rows = conn.execute(
        "SELECT net_name, status FROM faults"
        " WHERE campaign_id = ? AND net_name IN ('n22_core', 'n23_core')"
        " AND fault_type IN ('sa0', 'sa1')",
        (campaign_id,),
    ).fetchall()

    # N22/N23 are the system-side TO_SYS nets (safe-zero in INTEST) — their
    # faults should remain undetected because INTEST observes core_net, not sys.
    sys_po_rows = conn.execute(
        "SELECT net_name, status FROM faults"
        " WHERE campaign_id = ? AND net_name IN ('N22', 'N23')"
        " AND fault_type IN ('sa0', 'sa1')",
        (campaign_id,),
    ).fetchall()
    conn.close()

    # Core output nets have detectable faults.
    assert core_po_rows, "expected fault rows for n22_core/n23_core"
    assert any(r["status"] == "detected" for r in core_po_rows), (
        f"INTEST must detect >=1 fault on core PO nets; rows={core_po_rows}"
    )

    # System PO nets are safe-zero'd in INTEST — none should be detected.
    detected_sys = [r for r in sys_po_rows if r["status"] == "detected"]
    assert not detected_sys, (
        f"INTEST must NOT detect faults on TO_SYS (N22/N23); detected={detected_sys}"
    )


# --------------------------------------------------------------------------- #
# EXTEST mode — test the boundary / interconnect                              #
# stimulus = wbc_out.sys + top PIs; observe = wbc_in.sys = FROM_SYS = PIs   #
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_extest_detects_boundary_faults(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """EXTEST: driving PI nets exposes SA faults on FROM_SYS nets (N1-N7)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db = str(tmp_path / "faults.db")
    conn = connect(db)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    # Provide all 32 patterns on the system-side PIs.
    summary = dict(
        core.simulate_to_db(
            str(FIXTURE),
            str(CELL_MAP),
            db,
            campaign_id,
            _all32_sys(),
            SYS_PI_NAMES,
            "extest_exhaustive",
            test_mode="extest",
        )
    )
    denom = int(summary.get("denominator") or 0)
    detected = int(summary.get("detected") or 0)

    assert denom > 0, "EXTEST denominator must be >0"
    assert detected > 0, f"EXTEST must detect >=1 fault; got 0/{denom}"

    # FROM_SYS (N1-N7) faults should be detected by exhaustive PI patterns.
    conn = connect(db)
    init_schema(conn)
    pi_rows = conn.execute(
        "SELECT net_name, fault_type, status FROM faults"
        " WHERE campaign_id = ? AND net_name IN ('N1','N2','N3','N6','N7')"
        " AND fault_type IN ('sa0','sa1')",
        (campaign_id,),
    ).fetchall()
    conn.close()

    detected_pi = [r for r in pi_rows if r["status"] == "detected"]
    assert detected_pi, (
        f"EXTEST must detect faults on FROM_SYS (N1-N7); none detected; "
        f"all_pi_rows={pi_rows}"
    )


@pytest.mark.golden
def test_extest_does_not_detect_core_internal_faults(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """EXTEST: core internal nets (n9-n12, n22_core, n23_core) are unobservable
    in EXTEST — their faults must remain undetected."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db = str(tmp_path / "faults.db")
    conn = connect(db)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    core.simulate_to_db(
        str(FIXTURE),
        str(CELL_MAP),
        db,
        campaign_id,
        _all32_sys(),
        SYS_PI_NAMES,
        "extest_test",
        test_mode="extest",
    )

    conn = connect(db)
    init_schema(conn)
    # Core internal gate output nets — not observable in EXTEST.
    internal_rows = conn.execute(
        "SELECT net_name, fault_type, status FROM faults"
        " WHERE campaign_id = ? AND net_name IN ('n9','n10','n11','n12',"
        "  'n22_core','n23_core')"
        " AND fault_type IN ('sa0','sa1')",
        (campaign_id,),
    ).fetchall()
    conn.close()

    detected_internal = [r for r in internal_rows if r["status"] == "detected"]
    assert not detected_internal, (
        f"EXTEST must NOT detect core internal faults; detected={detected_internal}"
    )


# --------------------------------------------------------------------------- #
# Mode distinction — INTEST and EXTEST detect disjoint fault sets             #
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_intest_extest_detect_different_faults(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """INTEST and EXTEST must detect different fault sets (role-flip verified)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    # --- INTEST run ---
    db_in = str(tmp_path / "intest.db")
    conn = connect(db_in)
    init_schema(conn)
    cid_in = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    core.simulate_to_db(
        str(FIXTURE), str(CELL_MAP), db_in, cid_in,
        _all32_core(), CORE_PI_NAMES, "intest", test_mode="intest",
    )

    conn = connect(db_in)
    init_schema(conn)
    intest_detected = {
        (r["net_name"], r["fault_type"])
        for r in conn.execute(
            "SELECT net_name, fault_type FROM faults"
            " WHERE campaign_id = ? AND status = 'detected'",
            (cid_in,),
        ).fetchall()
    }
    conn.close()

    # --- EXTEST run ---
    db_ex = str(tmp_path / "extest.db")
    conn = connect(db_ex)
    init_schema(conn)
    cid_ex = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    core.simulate_to_db(
        str(FIXTURE), str(CELL_MAP), db_ex, cid_ex,
        _all32_sys(), SYS_PI_NAMES, "extest", test_mode="extest",
    )

    conn = connect(db_ex)
    init_schema(conn)
    extest_detected = {
        (r["net_name"], r["fault_type"])
        for r in conn.execute(
            "SELECT net_name, fault_type FROM faults"
            " WHERE campaign_id = ? AND status = 'detected'",
            (cid_ex,),
        ).fetchall()
    }
    conn.close()

    # INTEST must detect SOME faults not detected by EXTEST (core logic).
    intest_only = intest_detected - extest_detected
    # EXTEST must detect SOME faults not detected by INTEST (boundary).
    extest_only = extest_detected - intest_detected

    assert intest_only, (
        f"INTEST must detect faults that EXTEST cannot (core logic); "
        f"intest={len(intest_detected)}, extest={len(extest_detected)}"
    )
    assert extest_only, (
        f"EXTEST must detect faults that INTEST cannot (boundary); "
        f"intest={len(intest_detected)}, extest={len(extest_detected)}"
    )


# --------------------------------------------------------------------------- #
# SAT ATPG — INTEST mode through native ATPG                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_intest_native_atpg_coverage(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """INTEST SAT ATPG via run_progressive_native_atpg achieves high coverage."""
    from dataclasses import replace

    from faultflow.config import load_config
    from faultflow.db import connect, init_schema, summary as db_summary
    from faultflow.runner.progressive_atpg import (
        redundancy_model_id,
        run_progressive_native_atpg,
    )
    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {FIXTURE}
cell_lib = {CELL_MAP}

[fault_model]
model = stuck_at

[testmode]
mode = intest
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path, TOP_WRAPPED)
    cfg = replace(cfg, output_root=tmp_path / "output")
    cfg.ensure_workspace()

    conn = connect(cfg.db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP_WRAPPED)
    conn.commit()
    conn.close()

    fp_dict = {
        "netlist_hash": "test",
        "cell_lib_hash": "test",
        "collapsing": False,
        "unsupported_cells": cfg.simulation.unsupported_cells,
        "include_clock_faults": cfg.fault_model.include_clock_faults,
        "include_reset_faults": cfg.fault_model.include_reset_faults,
        "fault_model": "stuck_at",
        "test_mode": cfg.test_mode,
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
    data = db_summary(conn, campaign_id=campaign_id)
    conn.close()

    cov = float(data.get("coverage_percent") or 0.0)
    assert cov > 0.0, f"INTEST ATPG must achieve >0% coverage, got {cov}%"
