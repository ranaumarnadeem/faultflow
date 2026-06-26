"""End-to-end fault-collapsing check through the enumeration -> DB path.

The C++ collapser unit tests prove `collapse_primitive_faults` is correct; this
test proves the collapsing flag actually flows through `ensure_faults_enumerated`
into the campaign database, marking faults as collapsed. It uses the AOI21
fixture (a21oi), which has exactly two collapsible equivalence classes, so the
fresh-campaign fault set should gain exactly two collapsed faults when the flag
is on and none when it is off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.db import connect, init_schema

ROOT = Path(__file__).resolve().parents[2]
HD_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_a21oi.json"


def _enumerate(tmp_path: Path, collapsing: bool) -> tuple[int, int]:
    import _faultflow_core as core  # type: ignore[import-not-found]

    from db_v3_helpers import insert_campaign  # type: ignore[import-not-found]

    db_path = tmp_path / f"faults_{collapsing}.db"
    conn = connect(db_path)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top="tiny_a21oi")
    conn.commit()
    conn.close()

    core.ensure_faults_enumerated(
        str(FIXTURE),
        str(HD_CELL_MAP),
        str(db_path),
        campaign_id,
        False,
        False,
        collapsing,
        "fail",
    )

    conn = connect(db_path)
    init_schema(conn)
    total = conn.execute(
        "SELECT COUNT(*) FROM faults WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()[0]
    collapsed = conn.execute(
        "SELECT COUNT(*) FROM faults "
        "WHERE campaign_id = ? AND collapsed_into IS NOT NULL",
        (campaign_id,),
    ).fetchone()[0]
    conn.close()
    return int(total), int(collapsed)


@pytest.mark.golden
def test_collapsing_flag_marks_faults_in_db(
    tmp_path: Path, require_cpp_core: None
) -> None:
    total_off, collapsed_off = _enumerate(tmp_path, collapsing=False)
    total_on, collapsed_on = _enumerate(tmp_path, collapsing=True)

    # Same total fault sites enumerated either way (collapsing tags, never drops).
    assert total_on == total_off
    # AOI21 has two collapsible equivalence classes; the flag must take effect.
    assert collapsed_off == 0
    assert collapsed_on == 2
