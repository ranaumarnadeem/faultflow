"""M3: dynamic compaction (sim-verified cube packing).

Validates `compact_run_dynamic` against the reverse-order compactor on a real
campaign: it must never produce more vectors than reverse and its packed set must
detect every originally-detected fault (coverage-preserving by construction).
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from faultflow.db import connect, init_schema, latest_campaign_id
from faultflow.runner.compaction import (
    _detected_fault_ids,
    compact_run,
    compact_run_dynamic,
)
from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.integration
def test_dynamic_compaction_preserves_coverage_and_not_worse_than_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netlist = ROOT / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(ROOT / "tests/python/atpg"))
    from faultflow.config import load_config
    from test_progressive_atpg import _model_id, _run_atpg

    cfg_path = tmp_path / "c432.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 64
max_rounds = 20
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, top="c432")
    cfg = dataclasses.replace(cfg, output_root=tmp_path / "out")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # Generate the raw (uncompacted) ATPG vector set + campaign.
    vectors, _stats, run_id, _a, _s = _run_atpg(
        cfg, netlist, _model_id(), target_coverage=100.0
    )
    cellmap = str(CELL_MAP)
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        campaign_id = latest_campaign_id(conn, "comb")
        assert campaign_id is not None
        detected = set(_detected_fault_ids(conn, campaign_id))
    assert detected, "c432 should detect faults"

    common = dict(
        json_path=str(netlist),
        cell_map_path=cellmap,
        db_path=str(cfg.db_path),
        campaign_id=campaign_id,
        run_id=run_id,
        vectors=vectors,
        unsupported="fail",
    )
    rev_vs, _r, _ = compact_run(**common)  # type: ignore[arg-type]
    # M4: multi-order packing (keeps the fewest-vector result over 3 orders).
    dyn_vs, _d, _ = compact_run_dynamic(  # type: ignore[arg-type]
        pack_orders=3, **common
    )

    # Dynamic is never worse than reverse (it merges, or falls back to reverse).
    assert dyn_vs.count <= rev_vs.count

    # Coverage preserved: the packed set detects every originally-detected fault.
    core = _load_core()
    covered: set[int] = set()
    for vec in dyn_vs.vectors:
        covered |= set(
            core.compaction_detections(
                str(netlist),
                cellmap,
                str(cfg.db_path),
                vec,
                vectors.input_order,
                sorted(detected),
                "fail",
            )
        )
    assert detected.issubset(covered)
