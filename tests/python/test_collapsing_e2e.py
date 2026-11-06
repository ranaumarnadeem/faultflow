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


def _enumerate(
    tmp_path: Path, collapsing: bool, **excluded_sites: set[str]
) -> tuple[int, int]:
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
        **excluded_sites,
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


@pytest.mark.golden
@pytest.mark.parametrize(
    ("argument", "exclusion"),
    [("clock_sites", "clock"), ("reset_sites", "reset")],
)
def test_no_fault_collapses_into_a_site_the_flow_excludes(
    tmp_path: Path, require_cpp_core: None, argument: str, exclusion: str
) -> None:
    """A flow's own exclusions (the scan flow's clock paths and settled flops) come
    before collapsing, so the collapser's rule holds for them too: B1 SA1 stays its
    own fault instead of disappearing into Y SA0, which nothing grades."""
    total, collapsed = _enumerate(tmp_path, True, **{argument: {"net:5:stem"}})
    assert total == 8
    assert collapsed == 1  # A2 SA0 into A1 SA0: neither is excluded

    conn = connect(tmp_path / "faults_True.db")
    rows = {
        (str(key), str(kind).lower()): (str(status), str(excl), into)
        for key, kind, status, excl, into in conn.execute(
            "SELECT fault_site_key, fault_type, status, exclusion, collapsed_into "
            "FROM faults"
        ).fetchall()
    }
    conn.close()
    assert rows[("net:4:stem", "sa1")] == ("undetected", "none", None)
    for kind in ("sa0", "sa1"):
        assert rows[("net:5:stem", kind)] == ("excluded", exclusion, None)
