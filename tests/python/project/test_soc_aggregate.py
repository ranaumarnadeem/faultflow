"""The chip number of a faultflow_project_v2 SoC (faultflow.project.soc_aggregate):
faults counted once by their identities, and the guards."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from faultflow.db import connect, init_schema
from faultflow.project.identity import SOC, Identity
from faultflow.project.soc_aggregate import AggregateError, Scope, aggregate_soc

# (site key, fault type, status, exclusion, collapsed)
Fault = tuple[str, str, str, str, bool]


def _db(path: Path, campaign_type: str, faults: list[Fault]) -> Path:
    with connect(path) as conn:
        init_schema(conn)
        cursor = conn.execute(
            "INSERT INTO campaigns (campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, "
            "collapsing, unsupported_cells, include_clock_faults, "
            "include_reset_faults) VALUES (?, 't', '', '', '', '', '', '', 0, "
            "'fail', 0, 0)",
            (campaign_type,),
        )
        campaign = cursor.lastrowid
        for key, fault_type, status, exclusion, collapsed in faults:
            conn.execute(
                "INSERT INTO faults (campaign_id, fault_site_key, net_id, net_name, "
                "compiled_net_index, fault_type, status, exclusion, collapsed_into) "
                "VALUES (?, ?, 0, '', 0, ?, ?, ?, ?)",
                (
                    campaign,
                    key,
                    fault_type,
                    status,
                    exclusion,
                    1 if collapsed else None,
                ),
            )
        conn.commit()
    return path


def _block(tmp_path: Path, name: str, faults: list[Fault]) -> Scope:
    path = _db(tmp_path / f"{name}.sqlite", "scan", faults)
    return Scope(name, path, "scan", lambda key: [(name, key)])


def _soc(
    tmp_path: Path, faults: list[Fault], identities: dict[str, list[Identity]]
) -> Scope:
    path = _db(tmp_path / "soc.sqlite", "scan_extest", faults)
    return Scope(
        SOC, path, "scan_extest", lambda key: identities.get(key, [(SOC, key)])
    )


def test_a_wire_between_two_blocks_is_one_fault_both_blocks_leave_to_the_soc(
    tmp_path: Path,
) -> None:
    """A's port y and B's port a are one SoC net, 9: each block leaves its port
    stem to EXTEST, which grades it once."""
    block_a = _block(
        tmp_path,
        "A",
        [
            ("net:1:stem", "sa0", "detected", "none", False),
            ("net:2:stem", "sa0", "excluded", "wbr_decoupled", False),
        ],
    )
    block_b = _block(
        tmp_path,
        "B",
        [
            ("net:3:stem", "sa0", "undetected", "none", False),
            ("net:4:stem", "sa0", "excluded", "wbr_decoupled", False),
            ("net:5:stem", "sa0", "excluded", "clock", False),
        ],
    )
    soc = _soc(
        tmp_path,
        [
            ("net:9:stem", "sa0", "detected", "none", False),
            ("net:10:stem", "sa0", "detected", "none", False),
            ("net:11:stem", "sa0", "excluded", "wbr_decoupled", False),
        ],
        {
            "net:9:stem": [("A", "net:2:stem"), ("B", "net:4:stem")],
            "net:11:stem": [("A", "net:1:stem")],
        },
    )
    chip = aggregate_soc("soc2", [block_a, block_b, soc])
    assert (chip.chip_denominator, chip.chip_detected) == (4, 3)
    assert chip.chip_coverage_percent == 75.0
    assert [s.owned for s in chip.scopes] == [1, 1, 2]
    assert chip.guards["no_fault_counted_twice"]


def test_a_fault_counted_twice_or_left_to_no_one_is_refused(tmp_path: Path) -> None:
    block = _block(tmp_path, "A", [("net:1:stem", "sa0", "detected", "none", False)])
    both = _soc(
        tmp_path,
        [("net:9:stem", "sa0", "detected", "none", False)],
        {"net:9:stem": [("A", "net:1:stem")]},
    )
    with pytest.raises(AggregateError, match="counted by two scopes"):
        aggregate_soc("soc2", [block, both])
    left = tmp_path / "left"
    left.mkdir()
    block = _block(
        left, "A", [("net:2:stem", "sa1", "excluded", "wbr_decoupled", False)]
    )
    nobody = _soc(left, [("net:9:stem", "sa1", "detected", "none", False)], {})
    with pytest.raises(AggregateError, match="no other scope grades"):
        aggregate_soc("soc2", [block, nobody])
    # Unless the glue ties it, or the SoC accounts for it by design.
    assert aggregate_soc("soc2", [block, nobody], tied=[("A", "net:2:stem")])
    excluded = left / "excluded"
    excluded.mkdir()
    by_design = _soc(
        excluded,
        [
            ("net:9:stem", "sa1", "detected", "none", False),
            ("net:8:stem", "sa1", "excluded", "clock", False),
        ],
        {"net:8:stem": [("A", "net:2:stem")]},
    )
    assert aggregate_soc("soc2", [block, by_design]).chip_denominator == 1
    with pytest.raises(AggregateError, match="aren't distinct"):
        aggregate_soc("soc2", [block, block])


def test_a_scope_without_its_campaign_is_refused(tmp_path: Path) -> None:
    path = _db(tmp_path / "x.sqlite", "scan", [])
    scope = Scope("A", path, "scan_extest", lambda key: [("A", key)])
    with pytest.raises(AggregateError, match="no scan_extest run"):
        aggregate_soc("soc2", [scope])
    assert sqlite3.connect(path)
