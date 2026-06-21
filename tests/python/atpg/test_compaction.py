from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.db import connect, init_schema, summary
from faultflow.runner import Runner
import faultflow.runner.runner as runner_mod

ROOT = Path(__file__).resolve().parents[3]
C17_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
SKY130_CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"


# --------------------------------------------------------------------------
# Fast config-validation tests (no simulation).
# --------------------------------------------------------------------------
def _cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.ofs"
    path.write_text(body, encoding="utf-8")
    return path


def test_compaction_default_is_reverse(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n")
    assert load_config(cfg, "top").atpg.compaction == "reverse"


@pytest.mark.parametrize("value", ["none", "reverse"])
def test_compaction_accepts_valid(tmp_path: Path, value: str) -> None:
    cfg = _cfg(
        tmp_path,
        f"[design]\nnetlist = n.json\ncell_lib = c.json\n"
        f"[atpg]\ncompaction = {value}\n",
    )
    assert load_config(cfg, "top").atpg.compaction == value


def test_compaction_rejects_invalid(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        "[design]\nnetlist = n.json\ncell_lib = c.json\n"
        "[atpg]\ncompaction = sometimes\n",
    )
    with pytest.raises(ConfigError, match="compaction"):
        load_config(cfg, "top")


# --------------------------------------------------------------------------
# Integration: coverage invariance + reduction through a real sim.
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def require_cpp_core() -> None:
    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")


def _prepare_workspace(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workdir)
    schema_dir = workdir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        ROOT / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )


def _write_cfg(
    workdir: Path, *, netlist: Path, compaction: str, model: str = "stuck_at"
) -> Path:
    cfg_path = workdir / "config.ofs"
    cfg_path.write_text(
        (f"""
[design]
netlist = {netlist}
cell_lib = {SKY130_CELL_LIB}
liberty = {SKY130_LIBERTY}

[fault_model]
collapsing = false
model = {model}

[simulation]
unsupported_cells = fail

[atpg]
tool = native
random_vectors = 64
max_rounds = 20
compaction = {compaction}

[report]
threshold = 95.0
""".strip() + "\n"),
        encoding="utf-8",
    )
    return cfg_path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    top: str,
    netlist: Path,
    compaction: str,
    tag: str,
    model: str = "stuck_at",
) -> tuple[dict, dict]:
    workdir = tmp_path / tag
    _prepare_workspace(workdir, monkeypatch)
    cfg_path = _write_cfg(workdir, netlist=netlist, compaction=compaction, model=model)
    runner = Runner(load_config(cfg_path, top))
    runner.init()
    runner.sim(clean=True, max_rounds=20, target_coverage=95.0)
    report = json.loads(
        (
            workdir / f"output/{top}/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
    return report, data


@pytest.mark.integration
@pytest.mark.slow
def test_compaction_preserves_coverage_and_reduces_c17(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C17_JSON.exists():
        pytest.skip("c17 sky130 netlist missing")

    rep_none, sum_none = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="none",
        tag="none",
    )
    rep_rev, sum_rev = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="reverse",
        tag="reverse",
    )

    # Coverage and fault accounting are unchanged by compaction.
    assert sum_rev["coverage_percent"] == sum_none["coverage_percent"]
    assert sum_rev["detected"] == sum_none["detected"]
    assert sum_rev["denominator"] == sum_none["denominator"]

    # The compacted set is strictly smaller for c17 (31 -> ~9) and reports as a
    # compacted run.
    assert rep_rev["run"]["vector_count"] < rep_none["run"]["vector_count"]
    assert rep_rev["run"]["vector_source"] == "compacted_native_sat_atpg"
    assert rep_none["run"]["vector_source"] == "native_sat_atpg"


@pytest.mark.integration
@pytest.mark.slow
def test_transition_compaction_preserves_coverage_c17(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C17_JSON.exists():
        pytest.skip("c17 sky130 netlist missing")

    rep_none, sum_none = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="none",
        tag="trans_none",
        model="transition",
    )
    rep_rev, sum_rev = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="reverse",
        tag="trans_reverse",
        model="transition",
    )

    # Two-frame compaction preserves transition coverage exactly.
    assert sum_rev["coverage_percent"] == sum_none["coverage_percent"]
    assert sum_rev["detected"] == sum_none["detected"]
    assert sum_rev["denominator"] == sum_none["denominator"]
    # And reduces the launch/capture pair count.
    assert rep_rev["run"]["vector_count"] <= rep_none["run"]["vector_count"]
    assert rep_rev["summary"].get("fault_model") == "transition"
    assert rep_rev["run"]["vector_source"] == "compacted_native_transition_atpg"
    assert rep_none["run"]["vector_source"] == "native_transition_atpg"


@pytest.mark.integration
@pytest.mark.slow
def test_compaction_is_deterministic_c17(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C17_JSON.exists():
        pytest.skip("c17 sky130 netlist missing")

    rep_a, _ = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="reverse",
        tag="a",
    )
    rep_b, _ = _run(
        tmp_path,
        monkeypatch,
        top="c17",
        netlist=C17_JSON,
        compaction="reverse",
        tag="b",
    )
    assert rep_a["run"]["vector_count"] == rep_b["run"]["vector_count"]
