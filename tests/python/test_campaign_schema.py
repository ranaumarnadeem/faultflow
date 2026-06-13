import pytest

from faultflow.db import SchemaError, connect, init_schema, summary
from db_v3_helpers import insert_campaign, insert_fault_row, insert_run


def test_legacy_schema_rejected(tmp_path) -> None:
    db = tmp_path / "legacy.sqlite"
    with connect(db) as conn:
        conn.execute("""
            CREATE TABLE faults (
              id INTEGER PRIMARY KEY,
              net_id INTEGER,
              compiled_net_index INTEGER,
              fault_type TEXT
            )
            """)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        with pytest.raises(SchemaError, match="Legacy database schema"):
            init_schema(conn)


def test_campaign_scoped_summary(tmp_path) -> None:
    db = tmp_path / "v3.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        campaign_id = insert_campaign(conn)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="detected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa1",
            status="undetected",
        )
        data = summary(conn, campaign_id=campaign_id)
    assert data["detected"] == 1
    assert data["denominator"] == 2


def test_composite_vector_fk_campaign_scope(tmp_path) -> None:
    db = tmp_path / "fk.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        comb = insert_campaign(conn, campaign_type="comb")
        scan = insert_campaign(conn, campaign_type="scan", top="scan_top")
        run_id = insert_run(conn, comb)
        conn.execute(
            """
            INSERT INTO vectors(campaign_id, run_id, source, vector_index, pattern)
            VALUES (?, ?, 'native_sat_atpg', 1, '0')
            """,
            (comb, run_id),
        )
        vector_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO atpg_candidates(
                  campaign_id, run_id, candidate_id, pattern, source, status,
                  accepted_vector_id
                ) VALUES (?, ?, 1, '0', 'sat', 'accepted', ?)
                """,
                (scan, run_id, vector_id),
            )
            conn.commit()
