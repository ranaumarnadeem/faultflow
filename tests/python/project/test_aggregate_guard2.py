"""Guard 2 (double-count) has zero negative-path coverage elsewhere: every existing
test only exercises the passing case (disjoint ownership). This drives the guard
directly with a fabricated fixture -- no real Yosys/ATPG run needed.

Ownership is keyed by the canonical, net-id-independent identity from
`_canonical_key`. For a WBC pin, that key is
``("wbc", boundary_block, boundary_wbc, pin, side, fault_type)`` -- it does NOT
include the scope name, precisely so a handed-off boundary fault is recognised as
the *same* site whether seen from the block or the assembly netlist. That is also
exactly the hole Guard 2 exists to close: if the SAME boundary id
(``boundary_block``/``boundary_wbc``/``pin``) is tagged onto a WBC-inward cell in
TWO different "block" scopes, both scopes' `_owning_role` resolves to "block" (the
owning role for inward pins is unconditional), so BOTH chip-own the identical
canonical key -- an illegitimate double count that would inflate the chip
denominator. Guard 2 must catch this before the numbers are trusted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.db.sqlite import init_schema
from faultflow.project.aggregate import AggregateError, aggregate_project
from faultflow.project.orchestrator import ScopeRun

_CAMPAIGN_DEFAULTS = dict(
    netlist_hash="h",
    cell_lib_hash="h",
    config_hash="h",
    template_hash="h",
    yosys_version="0",
    faultflow_version="0",
    collapsing=1,
    unsupported_cells="fail",
    include_clock_faults=0,
    include_reset_faults=0,
)


def _netlist(tmp: Path, name: str, top: str, cells: dict) -> Path:
    p = tmp / f"{name}.json"
    p.write_text(
        json.dumps({"modules": {top: {"attributes": {}, "cells": cells}}}),
        encoding="utf-8",
    )
    return p


def _wbc_in_cell(net: int, *, block: str, wbc: str) -> dict:
    """A WBC-INWARD cell (TO_CORE) tagged with an explicit boundary id, exactly as
    graybox composition would tag it -- so two independently-built "block" scopes
    can be made to claim the identical boundary id."""
    return {
        "type": "$wbc_in_scan_faultflow",
        "attributes": {"faultflow_block": block, "faultflow_wbc": wbc},
        "connections": {"TO_CORE": [net]},
    }


def _make_db(path: Path, top: str, campaign_type: str, faults: list[dict]) -> None:
    import faultflow.db as dbmod

    with dbmod.connect(path) as conn:
        init_schema(conn)
        cur = conn.execute(
            "INSERT INTO campaigns (campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, collapsing, "
            "unsupported_cells, include_clock_faults, include_reset_faults) VALUES "
            "(:campaign_type, :top, :netlist_hash, :cell_lib_hash, :config_hash, "
            ":template_hash, :yosys_version, :faultflow_version, :collapsing, "
            ":unsupported_cells, :include_clock_faults, :include_reset_faults)",
            {"campaign_type": campaign_type, "top": top, **_CAMPAIGN_DEFAULTS},
        )
        campaign_id = cur.lastrowid
        for f in faults:
            conn.execute(
                "INSERT INTO faults (campaign_id, fault_site_key, net_id, net_name, "
                "compiled_net_index, fault_type, status, exclusion, collapsed_into) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    f["fault_site_key"],
                    f["net_id"],
                    f.get("net_name", f["fault_site_key"]),
                    f["net_id"],
                    f["fault_type"],
                    f["status"],
                    f.get("exclusion", "none"),
                    f.get("collapsed_into"),
                ),
            )
        conn.commit()


def _build_double_owned_scopes(tmp_path: Path) -> list[ScopeRun]:
    """Two DIFFERENT "block" scopes whose netlists both tag a WBC-inward cell with
    the SAME (boundary_block, boundary_wbc) pair -- so both chip-own the identical
    canonical ("wbc", "shared_block", "shared_wbc", "TO_CORE", "in", "sa0") key."""
    first_json = _netlist(
        tmp_path,
        "first",
        "blkA",
        {"__wi_x": _wbc_in_cell(5, block="shared_block", wbc="shared_wbc")},
    )
    first_db = tmp_path / "first.sqlite"
    _make_db(
        first_db,
        "blkA",
        "scan",
        [
            {
                "fault_site_key": "net:5:stem",
                "net_id": 5,
                "fault_type": "sa0",
                "status": "detected",
                "exclusion": "none",
            },
        ],
    )

    second_json = _netlist(
        tmp_path,
        "second",
        "blkB",
        {"__wi_x": _wbc_in_cell(11, block="shared_block", wbc="shared_wbc")},
    )
    second_db = tmp_path / "second.sqlite"
    _make_db(
        second_db,
        "blkB",
        "scan",
        [
            {
                "fault_site_key": "net:11:stem",
                "net_id": 11,
                "fault_type": "sa0",
                "status": "detected",
                "exclusion": "none",
            },
        ],
    )

    return [
        ScopeRun("block", "first", "blkA", "scan", first_db, first_json, (), ""),
        ScopeRun("block", "second", "blkB", "scan", second_db, second_json, (), ""),
    ]


def test_double_owned_wbc_key_is_not_yet_guarded_without_fixture() -> None:
    """Sanity check on the fixture-construction idea itself (not a real assertion of
    the guard): two scopes with genuinely disjoint canonical keys must NOT raise.
    This pins down that `aggregate_project` only raises when keys truly collide,
    ruling out a trivially-always-raising fixture before we lean on it below."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scopes = _build_double_owned_scopes(tmp_path)
        # Give the two scopes DISTINCT boundary ids -> must NOT collide.
        distinct_first = json.loads(scopes[0].netlist.read_text(encoding="utf-8"))
        distinct_first["modules"]["blkA"]["cells"]["__wi_x"]["attributes"][
            "faultflow_wbc"
        ] = "only_in_first"
        scopes[0].netlist.write_text(json.dumps(distinct_first), encoding="utf-8")
        chip = aggregate_project("p", scopes)
        assert chip.guards["no_double_count"] is True


def test_aggregate_rejects_double_owned_wbc_fault(tmp_path: Path) -> None:
    """The actual Guard 2 negative path: two block scopes both chip-own the SAME
    canonical WBC key -> AggregateError, with the offending key + both scope names
    reported in the message."""
    scopes = _build_double_owned_scopes(tmp_path)
    with pytest.raises(AggregateError, match="double-counted") as excinfo:
        aggregate_project("p", scopes)
    message = str(excinfo.value)
    assert "shared_block" in message
    assert "shared_wbc" in message
    assert "first" in message
    assert "second" in message
