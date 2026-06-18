from __future__ import annotations

from faultflow.db import connect, init_schema
from faultflow.db.candidates import (
    CandidateCommit,
    CandidateRejection,
    append_vector_row,
    commit_candidate,
    insert_pending_candidate,
    load_blocked_patterns,
)
from db_v3_helpers import insert_campaign, insert_fault_row, insert_run


def test_commit_candidate_writes_vector_index_not_row_id(tmp_path) -> None:
    db = tmp_path / "candidates.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, campaign_id, vector_source="scan_native_sat_atpg")
        insert_fault_row(
            conn,
            campaign_id,
            net_id=10,
            net_name="n10",
            compiled_net_index=3,
            fault_type="sa0",
            status="undetected",
            fault_site_key="net:10:stem",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        insert_pending_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=1,
            pattern="01",
            source="sat",
            sat_target_fault_id=fault_id,
        )
        vector_index = 2
        vector_id = append_vector_row(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            source="scan_native_sat_atpg",
            vector_index=vector_index,
            pattern="01",
        )
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=1,
            commit=CandidateCommit(
                status="accepted",
                vector_index=vector_index,
                accepted_vector_id=vector_id,
                detections=[fault_id],
            ),
        )
        row = conn.execute(
            """
            SELECT vector_index
            FROM fault_detections
            WHERE fault_id = ? AND run_id = ?
            """,
            (fault_id, run_id),
        ).fetchone()
        assert row is not None
        assert int(row["vector_index"]) == vector_index
        assert int(row["vector_index"]) != vector_id


def test_load_blocked_patterns_groups_by_fault(tmp_path) -> None:
    db = tmp_path / "blocked.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        insert_fault_row(
            conn,
            campaign_id,
            net_id=5,
            net_name="n5",
            compiled_net_index=5,
            fault_type="sa0",
            status="undetected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=7,
            net_name="n7",
            compiled_net_index=7,
            fault_type="sa1",
            status="undetected",
        )
        fault_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM faults ORDER BY net_id").fetchall()
        ]
        conn.execute(
            """
            INSERT INTO blocked_patterns(campaign_id, fault_id, pattern)
            VALUES (?, ?, '01'), (?, ?, '10'), (?, ?, '11')
            """,
            (
                campaign_id,
                fault_ids[0],
                campaign_id,
                fault_ids[0],
                campaign_id,
                fault_ids[1],
            ),
        )
        conn.commit()
        blocked = load_blocked_patterns(conn, campaign_id)
    assert blocked[fault_ids[0]] == {"01", "10"}
    assert blocked[fault_ids[1]] == {"11"}


def test_rejected_candidate_records_protocol_sim_rejections(tmp_path) -> None:
    db = tmp_path / "reject.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, campaign_id, vector_source="scan_native_sat_atpg")
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="undetected",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        insert_pending_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=2,
            pattern="00",
            source="sat",
            sat_target_fault_id=fault_id,
        )
        commit_candidate(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=2,
            commit=CandidateCommit(
                status="rejected",
                vector_index=1,
                protocol_sim_rejections=[
                    CandidateRejection(fault_id, "no_capture_or_unload_effect")
                ],
                blocked_patterns=[(fault_id, "00")],
            ),
        )
        rows = conn.execute(
            """
            SELECT reason_code
            FROM candidate_rejections
            WHERE campaign_id = ? AND candidate_id = 2
            """,
            (campaign_id,),
        ).fetchall()
        blocked = load_blocked_patterns(conn, campaign_id)
    assert [row["reason_code"] for row in rows] == ["no_capture_or_unload_effect"]
    assert blocked[fault_id] == {"00"}
