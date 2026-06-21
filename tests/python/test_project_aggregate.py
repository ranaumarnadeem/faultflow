"""End-to-end Stage 1: hierarchical project aggregation on the soc2 fixture.

Runs INTEST on two wrapped+scan blocks (block-as-top) + a combinational EXTEST on
the assembly with the cores blackboxed, then aggregates one chip coverage number
and checks the ownership guards. No external tools — gated on the C++ core.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.db import connect, latest_campaign_id
from faultflow.project.aggregate import AggregateError, aggregate_project
from faultflow.project.manifest import ProjectError, load_project
from faultflow.service.flow import FlowService
from soc2_fixtures import write_soc2


def test_load_project_validates_schema(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "nope", "blocks": []}), encoding="utf-8")
    with pytest.raises(ProjectError, match="unsupported project schema"):
        load_project(bad)


def test_load_project_rejects_assembly_top_collision(tmp_path: Path) -> None:
    manifest_path = write_soc2(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["interconnect"]["assembly_top"] = "blkA"  # collide with a block top
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError, match="assembly_top"):
        load_project(manifest_path)


@pytest.mark.golden
def test_project_aggregate_chip_coverage(
    tmp_path: Path, require_cpp_core: None
) -> None:
    manifest_path = write_soc2(tmp_path)

    # Drive the real per-block INTEST + assembly EXTEST + aggregation.
    result = FlowService().run_project(manifest_path)
    assert "project complete" in result.message

    # (1) Each block produced an isolated scan campaign.
    out = tmp_path / "output" / "soc2"
    block_dbs = {
        "blkA": out / "blkA" / "blkA" / ".faultflow" / "faultflow.sqlite",
        "blkB": out / "blkB" / "blkB" / ".faultflow" / "faultflow.sqlite",
    }
    for name, db in block_dbs.items():
        assert db.exists(), f"{name}: no block DB at {db}"
        with connect(db) as conn:
            assert latest_campaign_id(conn, "scan") is not None, f"{name}: no campaign"

    # (2) The SoC report exists with the additive chip number + guards.
    soc_json = out / "soc_coverage.json"
    assert soc_json.exists()
    report = json.loads(soc_json.read_text(encoding="utf-8"))
    assert report["schema"] == "faultflow_soc_coverage_v1"
    chip = report["chip"]
    scope_rows = report["scopes"]

    # (3) chip totals == Σ chip-OWNED per scope (not the per-scope summary
    #     denominators — the assembly's denominator includes block-owned WBC-inward
    #     faults it can observe, which the owning-role rule attributes to the block).
    assert chip["denominator"] == sum(s["owned"] for s in scope_rows)
    assert chip["detected"] == sum(s["owned_detected"] for s in scope_rows)
    assert chip["denominator"] > 0
    assert chip["coverage_percent"] is not None

    # (4) Three scopes: two blocks + the interconnect, all guards green.
    kinds = sorted(s["kind"] for s in scope_rows)
    assert kinds == ["block", "block", "interconnect"]
    g = report["guards"]
    assert g["tops_disjoint"] is True
    assert g["no_double_count"] is True
    assert g["partition_total"] is True
    assert g["handoff_complete"] is True  # the WBC ring is folded into the assembly

    # (5) Each scope's partition is total (every site owned/foreign/handoff/excluded).
    for s in scope_rows:
        assert (
            s["owned"] + s["foreign"] + s["handoff"] + s["excluded_by_design"]
            == s["total_sites"]
        )
    # (6) The assembly OWNS the blocks' wrapper-outward (handoff sites exist + matched).
    assert g["handoff_sites"] > 0


def test_aggregate_rejects_top_collision() -> None:
    """A duplicated scope top must fail the disjointness guard."""
    from faultflow.project.orchestrator import ScopeRun

    dup = [
        ScopeRun("block", "a", "same", "scan", Path("a.db"), Path("a.json"), (), ""),
        ScopeRun("block", "b", "same", "scan", Path("b.db"), Path("b.json"), (), ""),
    ]
    with pytest.raises(AggregateError, match="not pairwise distinct"):
        aggregate_project("p", dup)
