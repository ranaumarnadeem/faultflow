from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.db import connect, init_schema
from faultflow.runner import Runner
from faultflow.runner.progressive_atpg import (
    redundancy_model_id,
    run_progressive_transition_atpg,
    transition_pattern_key,
)
import faultflow.runner.runner as runner_mod
from campaign_fixtures import campaign_id_for_cfg

ROOT = Path(__file__).resolve().parents[3]
C17_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
TINY_INV_JSON = ROOT / "tests/cpp/fixtures/tiny_inv.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def require_cpp_core() -> None:
    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")


def _write_cfg(
    tmp_path: Path,
    *,
    netlist: Path,
    fault_model_lines: str,
    threshold: float = 100.0,
) -> Path:
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_LIB}

[fault_model]
{fault_model_lines}

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 64
max_rounds = 20
sat_timeout_seconds = 10
sat_conflict_limit = 100000
compaction = none

[report]
threshold = {threshold}
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg_path


def _run_transition_atpg(cfg, netlist, model_id: str, **kwargs: object):
    campaign_id = campaign_id_for_cfg(cfg, netlist)
    return run_progressive_transition_atpg(
        cfg, netlist, model_id, campaign_id=campaign_id, **kwargs
    )


# ---------------------------------------------------------------------------
# Config parsing / validation
# ---------------------------------------------------------------------------


def test_config_parses_transition_model(tmp_path: Path) -> None:
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\nlaunch = loc\ncollapsing = false",
    )
    cfg = load_config(cfg_path, "c17")
    assert cfg.fault_model.model == "transition"
    assert cfg.fault_model.launch == "loc"


def test_config_defaults_to_stuck_at(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, netlist=C17_JSON, fault_model_lines="")
    cfg = load_config(cfg_path, "c17")
    assert cfg.fault_model.model == "stuck_at"
    assert cfg.fault_model.launch == "loc"


def test_config_type_alias_back_compat(tmp_path: Path) -> None:
    # Legacy configs carried `type = stuck_at`; it remains accepted as an alias.
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="type = transition\ncollapsing = false",
    )
    cfg = load_config(cfg_path, "c17")
    assert cfg.fault_model.model == "transition"


def test_config_model_prefers_model_over_type(tmp_path: Path) -> None:
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\ntype = stuck_at\ncollapsing = false",
    )
    cfg = load_config(cfg_path, "c17")
    assert cfg.fault_model.model == "transition"


def test_config_rejects_invalid_model(tmp_path: Path) -> None:
    cfg_path = _write_cfg(
        tmp_path, netlist=C17_JSON, fault_model_lines="model = bridging"
    )
    with pytest.raises(ConfigError, match="stuck_at.*transition"):
        load_config(cfg_path, "c17")


def test_config_accepts_los_launch(tmp_path: Path) -> None:
    # LOS (launch-on-shift) is implemented and scan-only; it parses at config
    # load. The scan-only restriction is enforced at run time (Runner.sim).
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\nlaunch = los\ncollapsing = false",
    )
    cfg = load_config(cfg_path, "c17")
    assert cfg.fault_model.launch == "los"


def test_config_rejects_invalid_launch(tmp_path: Path) -> None:
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\nlaunch = bogus",
    )
    with pytest.raises(ConfigError, match="launch must be 'loc' or 'los'"):
        load_config(cfg_path, "c17")


def test_config_rejects_transition_with_collapsing(tmp_path: Path) -> None:
    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\ncollapsing = true",
    )
    with pytest.raises(ConfigError, match="collapsing is not supported"):
        load_config(cfg_path, "c17")


# ---------------------------------------------------------------------------
# Redundancy model isolation
# ---------------------------------------------------------------------------


def test_redundancy_model_id_differs_by_fault_model() -> None:
    base = {
        "netlist_hash": "n",
        "cell_lib_hash": "c",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
    }
    stuck = redundancy_model_id({**base, "fault_model": "stuck_at"})
    trans = redundancy_model_id({**base, "fault_model": "transition"})
    assert stuck != trans
    # Default (no fault_model key) matches explicit stuck_at.
    assert redundancy_model_id(base) == stuck


# ---------------------------------------------------------------------------
# Round-loop unit tests (tiny_inv, monkeypatched solver)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transition_colliding_sat_witness_does_not_permanently_stall_a_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """Regression for the transition-loop's copy of the same bug covered by
    test_progressive_atpg.py::test_colliding_sat_witness_does_not_permanently_stall_a_fault:
    when a fault's SAT witness (launch, capture) collides with a pattern
    already accepted for a different fault this run, `rejected_patterns` must
    be updated so the next solve attempt is forced away from the stale
    witness. Without that update the fault is trapped in `protocol_no_progress`
    forever with zero `fault_sat_outcome` rows despite being repeatedly
    (uselessly) re-attempted."""
    if not TINY_INV_JSON.exists():
        pytest.skip("tiny_inv fixture missing")
    monkeypatch.chdir(tmp_path)

    cfg_path = _write_cfg(
        tmp_path,
        netlist=TINY_INV_JSON,
        fault_model_lines="model = transition\nlaunch = loc\ncollapsing = false",
    )
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "random_vectors = 64", "random_vectors = 0"
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, "tiny_inv")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    netlist = cfg.output_dir / "tiny_inv.json"
    shutil.copy(TINY_INV_JSON, netlist)

    core = runner_mod._load_core()
    assert core is not None

    campaign_id = campaign_id_for_cfg(cfg, netlist)
    core.ensure_faults_enumerated(
        str(netlist),
        str(cfg.cell_lib),
        str(cfg.db_path),
        campaign_id,
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        cfg.simulation.unsupported_cells,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        row_sa1 = conn.execute(
            "SELECT id FROM faults WHERE net_name = 'Y' AND fault_type = 'sa1'"
        ).fetchone()
        row_sa0 = conn.execute(
            "SELECT id FROM faults WHERE net_name = 'Y' AND fault_type = 'sa0'"
        ).fetchone()
    assert row_sa1 is not None and row_sa0 is not None
    ids = [int(row_sa1["id"]), int(row_sa0["id"])]

    # Empirically find a genuinely-detecting (launch, capture) pair for each
    # fault over tiny_inv's single PI "A" -- ask the real verifier rather than
    # hand-deriving STR/STF polarity, since the sa1/sa0 <-> STR/STF mapping is
    # an implementation detail of ensure_faults_enumerated's row reuse.
    candidates = ({"A": True}, {"A": False})
    detect_pair: dict[int, tuple[dict[str, bool], dict[str, bool]]] = {}
    for fid in ids:
        for launch in candidates:
            for capture in candidates:
                if launch == capture:
                    continue
                if core.verify_transition_candidate(
                    str(netlist),
                    str(cfg.cell_lib),
                    str(cfg.db_path),
                    fid,
                    launch,
                    capture,
                    cfg.simulation.unsupported_cells,
                ):
                    detect_pair[fid] = (launch, capture)
                    break
            if fid in detect_pair:
                break
    assert len(detect_pair) == 2, "expected both Y/sa1 and Y/sa0 to be detectable"

    # active_ids is processed in id order -- the lower id is accepted first.
    fault_id_1, fault_id_2 = sorted(ids)
    l1, c1 = detect_pair[fault_id_1]
    l2, c2 = detect_pair[fault_id_2]
    assert (l1, c1) != (l2, c2)  # distinct witnesses, else the test proves nothing
    key1 = transition_pattern_key(l1, c1, ["A"])

    def colliding_transition_solve(*args: Any, **kwargs: Any) -> dict[str, object]:
        del kwargs
        fault_id = int(args[3])
        blocked = list(args[4])
        if fault_id == fault_id_1:
            return {"result": "SAT", "launch": dict(l1), "capture": dict(c1)}
        if fault_id == fault_id_2:
            if key1 in blocked:
                return {"result": "SAT", "launch": dict(l2), "capture": dict(c2)}
            # Deterministic collision: fault_id_1's witness, which does NOT
            # genuinely detect fault_id_2 (verify_transition_candidate is
            # real/unmocked -- this call never reaches it).
            return {"result": "SAT", "launch": dict(l1), "capture": dict(c1)}
        return {"result": "TIMEOUT", "launch": {}, "capture": {}}

    monkeypatch.setattr(core, "solve_transition_fault_atpg", colliding_transition_solve)
    monkeypatch.setattr(core, "atpg_random_vector_pairs", lambda *_a, **_k: [])

    fp = {
        "netlist_hash": "n",
        "cell_lib_hash": "c",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
        "fault_model": "transition",
    }
    _, stats, _, _, _ = _run_transition_atpg(
        cfg,
        netlist,
        redundancy_model_id(fp),
        max_rounds=5,
        target_coverage=100.0,
    )

    assert stats.protocol_no_progress_rounds >= 1

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        fault2 = conn.execute(
            "SELECT status FROM faults WHERE id = ?", (fault_id_2,)
        ).fetchone()
        outcomes2 = conn.execute(
            "SELECT COUNT(*) AS n FROM fault_sat_outcome WHERE fault_id = ?",
            (fault_id_2,),
        ).fetchone()
    assert fault2 is not None
    assert fault2["status"] == "detected"  # pre-fix: stuck "undetected" forever
    assert outcomes2 is not None and int(outcomes2["n"]) == 0


# ---------------------------------------------------------------------------
# End-to-end transition campaign on c17
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_transition_c17_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C17_JSON.exists():
        pytest.skip("c17 netlist missing")

    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        ROOT / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )

    cfg_path = _write_cfg(
        tmp_path,
        netlist=C17_JSON,
        fault_model_lines="model = transition\ncollapsing = false",
        threshold=100.0,
    )
    runner = Runner(load_config(cfg_path, "c17"))
    runner.init()
    runner.sim(clean=True, max_rounds=20, target_coverage=100.0)

    report_path = tmp_path / "output/c17/.faultflow/intermediate/coverage_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Tagged as transition; accounting invariant holds exactly as for stuck-at.
    assert report["summary"]["fault_model"] == "transition"
    assert report["run"]["vector_source"] == "native_transition_atpg"
    data = report["summary"]
    invariant = (
        data["denominator"]
        + data["redundant"]
        + data["collapsed"]
        + data["excluded_blackbox"]
        + data["excluded_clock"]
        + data["excluded_reset"]
        + data["excluded_scan"]
    )
    assert data["total_raw_faults"] == invariant
    assert float(data["coverage_percent"] or 0.0) > 0.0

    # Undetected transition faults are reported as str/stf, not sa0/sa1.
    for fault in report["undetected_faults"]:
        assert fault["fault_type"] in {"str", "stf"}

    # Two-frame vectors persist a launch frame in launch_pattern.
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT pattern, launch_pattern FROM vectors ORDER BY id"
        ).fetchall()
    assert rows, "transition run produced no vectors"
    assert any(row["launch_pattern"] for row in rows)
    for row in rows:
        # When a launch frame is present it must match the capture-frame width.
        if row["launch_pattern"]:
            assert len(row["launch_pattern"]) == len(row["pattern"])
