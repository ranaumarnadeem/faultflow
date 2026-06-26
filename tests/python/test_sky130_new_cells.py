"""Sky130 HD cell-map additions for DSP and crypto cores.

Covers full/half adders, inverted-input logic gates, buffer/delay cells, and the
async-set flip-flop. Each case asserts the cell resolves to the intended GateType
(or FF metadata) with the correct ORDERED input pins — the order locks the
pin -> inN mapping the simulator relies on.

Pin facts verified against cells/sky130/sky130_fd_sc_hd.v:
  fa     : COUT, SUM, A, B, CIN     -> ADDF_S, S=SUM CO=COUT
  ha     : COUT, SUM, A, B          -> ADDH_S, S=SUM CO=COUT
  and2b  : X, A_N, B   X=~A_N & B   -> AND2B (in0 & ~in1) with inputs [B, A_N]
  or2b   : X, A, B_N   X=A | ~B_N   -> OR2B
  or4bb  : X, A, B, C_N, D_N         -> OR4BB
  and4bb : X, A_N, B_N, C, D          -> AND4BB (bubbles on the FIRST two inputs)
  a2bb2o : X, A1_N, A2_N, B1, B2      -> A2BB2O
  dfstp  : Q, CLK, D, SET_B           -> async set (active-low SET_B, sets Q=1)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.cell_map import load_cell_map, lookup_cell

ROOT = Path(__file__).resolve().parents[2]
HD_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def entries() -> list:
    return load_cell_map(HD_CELL_MAP)


GATE_CASES = [
    ("sky130_fd_sc_hd__fa_1", "ADDF", ["A", "B", "CIN"], {"S": "SUM", "CO": "COUT"}),
    ("sky130_fd_sc_hd__ha_1", "ADDH", ["A", "B"], {"S": "SUM", "CO": "COUT"}),
    ("sky130_fd_sc_hd__and2b_1", "AND2B", ["B", "A_N"], {"X": "X"}),
    ("sky130_fd_sc_hd__or2b_1", "OR2B", ["A", "B_N"], {"X": "X"}),
    ("sky130_fd_sc_hd__or4bb_1", "OR4BB", ["A", "B", "C_N", "D_N"], {"X": "X"}),
    ("sky130_fd_sc_hd__and4bb_1", "AND4BB", ["A_N", "B_N", "C", "D"], {"X": "X"}),
    ("sky130_fd_sc_hd__a2bb2o_1", "A2BB2O", ["A1_N", "A2_N", "B1", "B2"], {"X": "X"}),
    ("sky130_fd_sc_hd__clkinvlp_1", "INV", ["A"], {"Y": "Y"}),
    ("sky130_fd_sc_hd__bufinv_1", "INV", ["A"], {"Y": "Y"}),
    ("sky130_fd_sc_hd__bufbuf_1", "BUF", ["A"], {"X": "X"}),
    ("sky130_fd_sc_hd__dlygate4sd1_1", "BUF", ["A"], {"X": "X"}),
    ("sky130_fd_sc_hd__dlymetal6s2s_1", "BUF", ["A"], {"X": "X"}),
    ("sky130_fd_sc_hd__clkdlybuf4s15_1", "BUF", ["A"], {"X": "X"}),
]


@pytest.mark.parametrize("cell,gate_type,inputs,outputs", GATE_CASES)
def test_new_gate_cell_resolves(
    entries: list,
    cell: str,
    gate_type: str,
    inputs: list[str],
    outputs: dict[str, str],
) -> None:
    entry = lookup_cell(cell, entries)
    assert entry is not None, f"{cell} not in cell map"
    assert entry["node_type"] == "GATE"
    assert entry["gate_type"] == gate_type
    assert entry["inputs"] == inputs
    assert entry["outputs"] == outputs


@pytest.mark.golden
def test_full_adder_splits_into_sum_and_carry(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The full-adder cell is multi-output (SUM, COUT). Compilation must split it
    into an ADDF_S node (SUM) and an ADDF_CO node (COUT); both output nets must
    materialize and evaluate (detectable faults under exhaustive stimulus)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    from faultflow.db import connect, init_schema

    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    fixture = ROOT / "tests/fixtures/sky130/tiny_fa.json"
    db_path = tmp_path / "faults.db"
    conn = connect(db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top="tiny_fa")
    conn.commit()
    conn.close()

    vectors = [
        {"A": bool(i & 1), "B": bool(i & 2), "CIN": bool(i & 4)} for i in range(8)
    ]
    summary = core.simulate_to_db(
        str(fixture),
        str(HD_CELL_MAP),
        str(db_path),
        campaign_id,
        vectors,
        ["A", "B", "CIN"],
        "test",
    )
    assert summary["denominator"] > 0

    conn = connect(db_path)
    init_schema(conn)
    rows = conn.execute(
        "SELECT net_name, status FROM faults "
        "WHERE campaign_id = ? AND net_name IN ('SUM', 'COUT')",
        (campaign_id,),
    ).fetchall()
    conn.close()

    by_net: dict[str, list[str]] = {"SUM": [], "COUT": []}
    for r in rows:
        by_net.setdefault(str(r["net_name"]), []).append(str(r["status"]))
    # Both outputs must exist (split happened) and both must evaluate (detected).
    assert by_net["SUM"], "no faults on SUM — full-adder sum output not materialized"
    assert by_net[
        "COUT"
    ], "no faults on COUT — carry output not materialized (split failed)"
    assert any(
        s == "detected" for s in by_net["SUM"]
    ), "SUM evaluates as constant — ADDF_S split wrong"
    assert any(
        s == "detected" for s in by_net["COUT"]
    ), "COUT evaluates as constant — ADDF_CO split wrong"


def test_dfstp_resolves_as_async_set_ff(entries: list) -> None:
    entry = lookup_cell("sky130_fd_sc_hd__dfstp_1", entries)
    assert entry is not None, "dfstp not in cell map"
    assert entry["node_type"] == "FF"
    ff = entry["ff"]
    assert ff["clock"] == "CLK"
    assert ff["data"] == "D"
    assert ff["output"] == "Q"
    assert ff["trigger"] == "POSEDGE"
    preset = ff["preset"]
    assert preset["pin"] == "SET_B"
    assert preset["level"] == "LOW"
    assert preset["value"] == 1
