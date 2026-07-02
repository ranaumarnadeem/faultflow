from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.db import connect, init_schema
from faultflow.runner import Runner
from faultflow.runner.progressive_atpg import redundancy_model_id
import faultflow.runner.runner as runner_mod

ROOT = Path(__file__).resolve().parents[3]
C17_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
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
