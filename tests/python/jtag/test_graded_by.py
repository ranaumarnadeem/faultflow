"""The coverage report's ``graded_by`` block: which test grades which faults.

Scan ATPG grades the core and the inserted logic that is ordinary logic; the test
instruments (a JTAG TAP, the IJTAG network, an IEEE 1500 WSP) are tested by their own
programs (``ff.py jtag``, ``ff.py wsp``), and memories by MBIST. The block counts each
population, what its own test detected, and the chip's coverage with them all."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db_v3_helpers import insert_campaign, insert_fault_row
from faultflow.db import connect, init_schema, summary
from faultflow.jtag import store as jtag_store
from faultflow.reporter.graded_by import graded_by_block, graded_by_lines
from faultflow.wrap import wsp_grade

FAULTS = [
    # id: (type, status, exclusion)
    (1, "sa0", "detected", "none"),
    (2, "sa1", "undetected", "none"),
    (3, "sa0", "redundant", "none"),
    (4, "sa0", "undetected", "jtag"),
    (5, "sa1", "undetected", "jtag"),
    (6, "sa0", "undetected", "wsp"),
    (7, "sa0", "undetected", "reset"),
    (8, "sa0", "undetected", "none"),
    (9, "sa1", "redundant", "none"),
]


def _campaign(conn: sqlite3.Connection) -> int:
    campaign = insert_campaign(conn, campaign_type="scan")
    for net, fault_type, status, exclusion in FAULTS:
        insert_fault_row(
            conn,
            campaign,
            net_id=net,
            net_name=f"n{net}",
            compiled_net_index=net,
            fault_type=fault_type,
            status=status,
            exclusion=exclusion,
        )
    # A collapsed fault is counted as collapsed and nowhere else.
    insert_fault_row(
        conn,
        campaign,
        net_id=10,
        net_name="n10",
        compiled_net_index=10,
        fault_type="sa0",
        status="excluded",
        exclusion="none",
        collapsed_into=1,
    )
    conn.commit()
    return campaign


def _ids(conn: sqlite3.Connection, campaign: int) -> dict[int, int]:
    return {
        int(net): int(fault_id)
        for fault_id, net in conn.execute(
            "SELECT id, net_id FROM faults WHERE campaign_id = ?", (campaign,)
        )
    }


def _jtag_run(conn: sqlite3.Connection, campaign: int, detected: list[int]) -> None:
    jtag_store.ensure_tables(conn)
    conn.execute(
        "INSERT INTO jtag_runs(campaign_id, run_key, program_digest, tests, periods, "
        "graded, reset_path, holds) VALUES (?, 'k', 'd', '[]', 1, 1, 0, '[]')",
        (campaign,),
    )
    run = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.executemany(
        "INSERT INTO jtag_detections(run_id, fault_id, test, period) "
        "VALUES (?, ?, 't', 0)",
        [(run, fault) for fault in detected],
    )
    conn.commit()


def _wsp_run(conn: sqlite3.Connection, campaign: int, detected: list[int]) -> None:
    conn.executescript(wsp_grade._SCHEMA)
    conn.execute(
        "INSERT INTO wsp_runs(campaign_id, run_key, program_digest, tests, cycles, "
        "graded, init_dependent) VALUES (?, 'k', 'd', '[]', 1, 1, 0)",
        (campaign,),
    )
    run = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.executemany(
        "INSERT INTO wsp_detections(run_id, fault_id, test, cycle) "
        "VALUES (?, ?, 't', 0)",
        [(run, fault) for fault in detected],
    )
    conn.commit()


@pytest.mark.unit
def test_each_test_counts_the_faults_it_grades_and_what_it_detected(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db.sqlite") as conn:
        init_schema(conn)
        campaign = _campaign(conn)
        ids = _ids(conn, campaign)
        # jtag: fault 4 (left to it), fault 8 (scan's, undetected) and fault 9
        # (scan proved it redundant). wsp: fault 6.
        _jtag_run(conn, campaign, [ids[4], ids[8], ids[9]])
        _wsp_run(conn, campaign, [ids[6]])
        conn.row_factory = sqlite3.Row
        block = graded_by_block(conn, campaign, summary(conn, campaign_id=campaign))

    assert block["scan"] == {"faults": 5, "detected": 1}
    assert block["jtag"] == {"faults": 2, "detected": 1, "graded": True}
    assert block["wsp"] == {"faults": 1, "detected": 1, "graded": True}
    assert block["reset"] == {"faults": 1}
    # Detected by any test: 1 (scan), 4 and 8 and 9 (jtag), 6 (wsp). Redundant, and
    # no test detected it: 3. The denominator drops it; the collapsed fault is out.
    combined = block["combined"]
    assert combined == {
        "structural_eligible": 8,
        "denominator": 7,
        "detected": 5,
        "redundant": 1,
        "test_coverage_percent": pytest.approx(100.0 * 5 / 7),
    }


@pytest.mark.unit
def test_a_test_not_run_is_unknown_not_zero_and_there_is_no_combined_number(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db.sqlite") as conn:
        init_schema(conn)
        campaign = _campaign(conn)
        conn.row_factory = sqlite3.Row
        block = graded_by_block(conn, campaign, summary(conn, campaign_id=campaign))
    assert block["jtag"] == {"faults": 2, "detected": None, "graded": False}
    assert block["wsp"] == {"faults": 1, "detected": None, "graded": False}
    assert block["combined"] is None
    lines = "\n".join(graded_by_lines(block))
    assert "jtag" in lines and "not run" in lines


@pytest.mark.unit
def test_a_design_with_nothing_for_another_test_lists_no_such_test(
    tmp_path: Path,
) -> None:
    """No fault left to JTAG or a WSP: the block says scan grades everything, and
    names the memories MBIST tests."""
    with connect(tmp_path / "db.sqlite") as conn:
        init_schema(conn)
        campaign = insert_campaign(conn, campaign_type="scan")
        insert_fault_row(
            conn,
            campaign,
            net_id=1,
            net_name="n1",
            compiled_net_index=1,
            fault_type="sa0",
            status="detected",
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        block = graded_by_block(
            conn,
            campaign,
            summary(conn, campaign_id=campaign),
            memories=["u_mem", "u_sram"],
        )
    assert block["jtag"]["faults"] == 0 and block["wsp"]["faults"] == 0
    assert block["memories"] == {"instances": ["u_mem", "u_sram"]}
    lines = "\n".join(graded_by_lines(block))
    assert "memories" in lines and "MBIST" in lines and "u_mem" in lines
    assert "wsp" not in lines  # nothing left to a WSP: no line for it
