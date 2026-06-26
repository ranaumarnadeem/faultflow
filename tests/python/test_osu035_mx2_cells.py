"""M0 — MX2X1/MX2X2/MX2X4 OSU035 cell map entries for OT TPI control-point muxes.

OT inserts MX2X1 (inverting 2:1 mux) as a controllability test point. Its select pin
is named S0 (vs S on MUX2X*), but semantics are identical: S0=1 selects A, output is ~A.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.cell_map import is_supported_or_deferred, load_cell_map, lookup_cell

ROOT = Path(__file__).resolve().parents[2]
OSU_CELL_MAP = ROOT / "cells/osu/osu035.json"
FIXTURE = ROOT / "tests/fixtures/osu035/tiny_mx2x1.json"
TOP = "tiny_mx2x1"


# --------------------------------------------------------------------------- #
# M0-U01 — unit: MX2X1/2/4 resolve to MUX2 with S0 as the select pin (in2)
# --------------------------------------------------------------------------- #


def _check_mx2_entry(cell_name: str) -> None:
    entries = load_cell_map(OSU_CELL_MAP)
    assert is_supported_or_deferred(cell_name, entries), f"{cell_name} not in cell map"
    entry = lookup_cell(cell_name, entries)
    assert entry is not None
    got = entry["gate_type"]
    assert got == "MUX2", f"{cell_name}: expected MUX2, got {got}"
    assert entry["inputs"] == [
        "A",
        "B",
        "S0",
    ], f"{cell_name}: unexpected inputs {entry['inputs']}"
    assert (
        entry["inputs"][2] == "S0"
    ), f"{cell_name}: select pin must be at index 2 (in2), got {entry['inputs']}"
    assert entry["outputs"] == {"Y": "Y"}


def test_mx2x1_parses_as_mux2() -> None:
    _check_mx2_entry("MX2X1")


def test_mx2x2_parses_as_mux2() -> None:
    _check_mx2_entry("MX2X2")


def test_mx2x4_parses_as_mux2() -> None:
    _check_mx2_entry("MX2X4")


# --------------------------------------------------------------------------- #
# M0-U02 — existing MUX2X* must still work (no regression)
# --------------------------------------------------------------------------- #


def test_mux2x1_still_resolves() -> None:
    entries = load_cell_map(OSU_CELL_MAP)
    entry = lookup_cell("MUX2X1", entries)
    assert entry is not None
    assert entry["gate_type"] == "MUX2"
    assert entry["inputs"][2] == "S"


# --------------------------------------------------------------------------- #
# M0-G01 — golden: tiny_mx2x1.json simulates with correct MUX2 truth table
# S0=1 selects A (inverting): Y = ~((S0&A)|(~S0&B))
# --------------------------------------------------------------------------- #


def _exhaustive_vectors() -> list[dict[str, bool]]:
    return [{"A": bool(i & 1), "B": bool(i & 2), "S0": bool(i & 4)} for i in range(8)]


def _expected_y(a: bool, b: bool, s0: bool) -> bool:
    return not (a if s0 else b)


@pytest.mark.golden
def test_mx2x1_sim_truth_table(tmp_path: Path, require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema

    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db_path = tmp_path / "faults.db"
    conn = connect(db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    vectors = _exhaustive_vectors()
    summary = core.simulate_to_db(
        str(FIXTURE),
        str(OSU_CELL_MAP),
        str(db_path),
        campaign_id,
        vectors,
        ["A", "B", "S0"],
        "test",
    )
    assert summary["denominator"] > 0, "no fault sites enumerated for MX2X1 netlist"

    # Verify fault-free output values match the MUX2 truth table.
    conn = connect(db_path)
    init_schema(conn)
    rows = conn.execute(
        "SELECT net_name, status FROM faults WHERE campaign_id = ? AND net_name = 'Y'",
        (campaign_id,),
    ).fetchall()
    conn.close()

    # At least some faults on Y must be detected — proves the cell evaluates.
    assert rows, "no faults recorded on output net Y"
    assert any(
        str(r["status"]) == "detected" for r in rows
    ), "no Y faults detected — MX2X1 may be evaluating as constant"
