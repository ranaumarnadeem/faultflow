from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class CandidateRejection:
    fault_id: int
    reason_code: str


@dataclass
class CandidateCommit:
    status: str  # accepted | rejected
    vector_index: int = 0
    accepted_vector_id: int | None = None
    detections: list[int] = field(default_factory=list)
    protocol_sim_rejections: list[CandidateRejection] = field(default_factory=list)
    blocked_patterns: list[tuple[int, str]] = field(default_factory=list)
    reduced_view_rejections: list[CandidateRejection] = field(default_factory=list)


def load_blocked_patterns(
    conn: sqlite3.Connection, campaign_id: int
) -> dict[int, set[str]]:
    rows = conn.execute(
        """
        SELECT fault_id, pattern
        FROM blocked_patterns
        WHERE campaign_id = ?
        """,
        (campaign_id,),
    ).fetchall()
    blocked: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        blocked[int(row["fault_id"])].add(str(row["pattern"]))
    return dict(blocked)


def insert_pending_candidate(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    run_id: int,
    candidate_id: int,
    pattern: str,
    source: str,
    sat_target_fault_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO atpg_candidates(
          campaign_id, run_id, candidate_id, pattern, source, status,
          sat_target_fault_id
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (campaign_id, run_id, candidate_id, pattern, source, sat_target_fault_id),
    )


def _apply_candidate_commit(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    run_id: int,
    candidate_id: int,
    commit: CandidateCommit,
) -> None:
    for fault_id in commit.detections:
        conn.execute(
            """
            UPDATE faults
            SET status = 'detected', protocol_unresolved = 0
            WHERE id = ? AND campaign_id = ?
            """,
            (fault_id, campaign_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO fault_detections(
              fault_id, campaign_id, run_id, vector_index, obs_net
            ) VALUES (?, ?, ?, ?, NULL)
            """,
            (
                fault_id,
                campaign_id,
                run_id,
                commit.vector_index,
            ),
        )
    for rejection in commit.reduced_view_rejections + commit.protocol_sim_rejections:
        conn.execute(
            """
            INSERT INTO candidate_rejections(
              campaign_id, run_id, candidate_id, fault_id, reason_code
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                run_id,
                candidate_id,
                rejection.fault_id,
                rejection.reason_code,
            ),
        )
    for fault_id, pattern in commit.blocked_patterns:
        conn.execute(
            """
            INSERT OR IGNORE INTO blocked_patterns(
              campaign_id, fault_id, pattern
            ) VALUES (?, ?, ?)
            """,
            (campaign_id, fault_id, pattern),
        )
    conn.execute(
        """
        UPDATE atpg_candidates
        SET status = ?, accepted_vector_id = ?
        WHERE campaign_id = ? AND run_id = ? AND candidate_id = ?
        """,
        (
            commit.status,
            commit.accepted_vector_id,
            campaign_id,
            run_id,
            candidate_id,
        ),
    )


def commit_candidate(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    run_id: int,
    candidate_id: int,
    commit: CandidateCommit,
) -> None:
    if conn.in_transaction:
        _apply_candidate_commit(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        return
    conn.execute("BEGIN")
    try:
        _apply_candidate_commit(
            conn,
            campaign_id=campaign_id,
            run_id=run_id,
            candidate_id=candidate_id,
            commit=commit,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def append_vector_row(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    run_id: int,
    source: str,
    vector_index: int,
    pattern: str,
    launch_pattern: str = "",
) -> int:
    conn.execute(
        """
        INSERT INTO vectors(
          campaign_id, run_id, source, vector_index, pattern, launch_pattern
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (campaign_id, run_id, source, vector_index, pattern, launch_pattern),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
