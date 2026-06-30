from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from faultflow.db import connect, init_schema, summary
from faultflow.runner import Runner
from faultflow.runner.progressive_atpg import (
    redundancy_model_id,
    run_progressive_native_atpg,
)
from campaign_fixtures import campaign_id_for_cfg
from faultflow.runner.runner import FingerprintMismatchError, RunnerError
import faultflow.runner.runner as runner_mod

ROOT = Path(__file__).resolve().parents[3]


def _run_atpg(cfg, netlist, model: str, **kwargs: object):
    campaign_id = campaign_id_for_cfg(cfg, netlist)
    return run_progressive_native_atpg(
        cfg, netlist, model, campaign_id=campaign_id, **kwargs
    )


FIXTURES = ROOT / "tests/cpp/fixtures"
C17_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
# Sky130: BENCH file not used
# C17_BENCH = ROOT / "tests/benchmarks/iscas85/synth/c17.bench"
# C17_TEST = ROOT / "tests/benchmarks/iscas85/synth/c17atpg.test"
C432_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c432.json"
C499_JSON = ROOT / "tests/benchmarks/iscas85/synth_sky130/c499.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def require_cpp_core() -> None:
    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")


def _prepare_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        ROOT / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )


def _write_cfg(
    tmp_path: Path,
    *,
    top: str,
    netlist: Path,
    atpg_overrides: dict[str, int | str] | None = None,
    fault_model_lines: str = "collapsing = false",
    threshold: float = 100.0,
) -> Path:
    atpg = {
        "random_vectors": 64,
        "max_rounds": 20,
        "sat_timeout_seconds": 10,
        "sat_conflict_limit": 100000,
        # These tests assert raw-ATPG run counts and the native vector_source;
        # compaction (default reverse) is exercised separately in test_compaction.
        "compaction": "none",
    }
    if atpg_overrides is not None:
        atpg.update(atpg_overrides)
    atpg_lines = "\n".join(f"{key} = {value}" for key, value in atpg.items())
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
{atpg_lines}

[report]
threshold = {threshold}
""".strip() + "\n",
        encoding="utf-8",
    )
    return cfg_path


def _runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    top: str,
    netlist: Path,
    atpg_overrides: dict[str, int | str] | None = None,
    threshold: float = 100.0,
) -> Runner:
    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top=top,
        netlist=netlist,
        atpg_overrides=atpg_overrides,
        threshold=threshold,
    )
    return Runner(load_config(cfg_path, top))


@pytest.mark.integration
def test_runner_resume_increases_or_preserves_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C432_JSON.exists():
        pytest.skip("c432 netlist missing")

    runner = _runner(tmp_path, monkeypatch, top="c432", netlist=C432_JSON)
    runner.init()
    runner.sim(clean=True, max_rounds=1, target_coverage=50.0)

    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        after_first = summary(conn)
        fault_rows = conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert run_count == 1
    assert after_first["detected"] > 0

    runner.sim(max_rounds=5, target_coverage=100.0)

    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        after_second = summary(conn)
        fault_rows_after = conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0]
        run_count_after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert fault_rows_after == fault_rows
    assert run_count_after == 2
    assert after_second["detected"] >= after_first["detected"]
    assert float(after_second["coverage_percent"] or 0.0) >= float(
        after_first["coverage_percent"] or 0.0
    )


@pytest.mark.integration
def test_incremental_sat_matches_baseline_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    # I4: the incremental (IFC) solver must produce the SAME end-to-end fault
    # classification as the baseline — identical detected, redundant, denominator
    # and coverage. Only the per-fault test vectors may differ.
    if not C432_JSON.exists():
        pytest.skip("c432 netlist missing")

    def _run(sub: str, incremental: str) -> tuple[dict, int]:
        wd = tmp_path / sub
        wd.mkdir()
        schema_dir = wd / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            ROOT / "schemas/coverage.schema.json",
            schema_dir / "coverage.schema.json",
        )
        monkeypatch.chdir(wd)
        cfg_path = _write_cfg(
            tmp_path=wd,
            top="c432",
            netlist=C432_JSON,
            atpg_overrides={"incremental_sat": incremental},
        )
        runner = Runner(load_config(cfg_path, "c432"))
        runner.init()
        runner.sim(clean=True, max_rounds=20, target_coverage=100.0)
        with connect(runner.cfg.db_path) as conn:
            init_schema(conn)
            s = summary(conn)
            redundant = conn.execute(
                "SELECT COUNT(*) FROM faults WHERE status='redundant'"
            ).fetchone()[0]
        return s, int(redundant)

    base, base_red = _run("base", "false")
    inc, inc_red = _run("inc", "true")

    assert base["detected"] > 0
    assert inc["detected"] == base["detected"]
    assert inc_red == base_red
    assert inc["denominator"] == base["denominator"]
    assert float(inc["coverage_percent"] or 0.0) == float(
        base["coverage_percent"] or 0.0
    )


@pytest.mark.integration
def test_cone_ordering_coverage_matches_no_ordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    # T3: lazy per-wave cone ordering must produce the same final fault
    # classification as no ordering. Ordering changes the sequence in which
    # workers receive faults but must not change what is detected, what is
    # redundant, or the denominator.
    if not C17_JSON.exists():
        pytest.skip("c17 netlist missing")

    def _run(sub: str, ordering: str) -> tuple[dict, int]:
        wd = tmp_path / sub
        wd.mkdir()
        schema_dir = wd / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            ROOT / "schemas/coverage.schema.json",
            schema_dir / "coverage.schema.json",
        )
        monkeypatch.chdir(wd)
        cfg_path = _write_cfg(
            tmp_path=wd,
            top="c17",
            netlist=C17_JSON,
            atpg_overrides={"order_by_cone_size": ordering},
        )
        runner = Runner(load_config(cfg_path, "c17"))
        runner.init()
        runner.sim(clean=True, max_rounds=20, target_coverage=100.0)
        with connect(runner.cfg.db_path) as conn:
            init_schema(conn)
            s = summary(conn)
            redundant = conn.execute(
                "SELECT COUNT(*) FROM faults WHERE status='redundant'"
            ).fetchone()[0]
        return s, int(redundant)

    ordered, ordered_red = _run("ordered", "true")
    unordered, unordered_red = _run("unordered", "false")

    assert ordered["detected"] > 0
    assert ordered["denominator"] == unordered["denominator"]
    assert ordered_red == unordered_red
    assert (
        abs(
            float(ordered["coverage_percent"] or 0.0)
            - float(unordered["coverage_percent"] or 0.0)
        )
        <= 0.1
    )


@pytest.mark.integration
def test_runner_fingerprint_mismatch_blocks_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    netlist = tmp_path / "output/tiny_inv/tiny_inv.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    runner = _runner(tmp_path, monkeypatch, top="tiny_inv", netlist=netlist)
    runner.init()
    runner.sim(clean=True, max_rounds=1, target_coverage=100.0)

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "unsupported_cells = fail", "unsupported_cells = blackbox"
        ),
        encoding="utf-8",
    )
    runner = Runner(load_config(cfg_path, "tiny_inv"))

    with pytest.raises(FingerprintMismatchError, match="changed"):
        runner.sim(max_rounds=1, target_coverage=100.0)


@pytest.mark.integration
def test_real_circuit_timeouts_persist_without_redundant_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_chain.json"
    if not fixture.exists():
        pytest.skip("tiny_chain fixture missing")

    netlist = tmp_path / "output/tiny_chain/tiny_chain.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top="tiny_chain",
        netlist=netlist,
        atpg_overrides={
            "random_vectors": 0,
            "sat_conflict_limit": 0,
            "sat_timeout_seconds": 0,
        },
        threshold=100.0,
    )
    cfg = load_config(cfg_path, "tiny_chain")

    fp = {
        "netlist_hash": "n",
        "cell_lib_hash": "c",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
    }
    _, stats, _, _, _ = _run_atpg(
        cfg, netlist, redundancy_model_id(fp), max_rounds=2, target_coverage=100.0
    )

    assert stats.timeout > 0
    assert stats.terminal_reason in {"STALLED", "MAX_ROUNDS"}

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        timeout_faults = conn.execute(
            "SELECT COUNT(*) AS n FROM faults WHERE status = 'undetected'"
        ).fetchone()

    assert data["undetected"] > 0
    assert data["redundant"] == 0
    assert timeout_faults is not None
    assert int(timeout_faults["n"]) == data["undetected"]
    if data["detected"] == 0:
        assert stats.terminal_reason == "STALLED"


@pytest.mark.integration
def test_timeout_and_unknown_faults_never_marked_redundant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_chain.json"
    if not fixture.exists():
        pytest.skip("tiny_chain fixture missing")

    netlist = tmp_path / "output/tiny_chain/tiny_chain.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top="tiny_chain",
        netlist=netlist,
        atpg_overrides={
            "random_vectors": 0,
            "sat_conflict_limit": 0,
            "sat_timeout_seconds": 0,
        },
    )
    cfg = load_config(cfg_path, "tiny_chain")
    model = redundancy_model_id(
        {
            "netlist_hash": "n",
            "cell_lib_hash": "c",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        }
    )
    _run_atpg(cfg, netlist, model, max_rounds=1, target_coverage=100.0)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        bad = conn.execute("""
            SELECT COUNT(*) AS n FROM faults
            WHERE status = 'redundant'
               OR status NOT IN ('undetected', 'detected', 'redundant')
            """).fetchone()

    assert data["redundant"] == 0
    assert data["undetected"] > 0
    assert bad is not None
    assert int(bad["n"]) == 0


@pytest.mark.integration
def test_redundant_faults_excluded_from_denominator_tiny_const(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_const.json"
    if not fixture.exists():
        pytest.skip("tiny_const fixture missing")

    netlist = tmp_path / "output/tiny_const/tiny_const.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top="tiny_const",
        netlist=netlist,
        atpg_overrides={"random_vectors": 0},
    )
    cfg = load_config(cfg_path, "tiny_const")
    model = redundancy_model_id(
        {
            "netlist_hash": "n",
            "cell_lib_hash": "c",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        }
    )
    _, stats, _, _, _ = _run_atpg(cfg, netlist, model)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        redundant_rows = conn.execute("""
            SELECT net_name, fault_type, status, redundancy_model_id
            FROM faults
            WHERE status = 'redundant'
            ORDER BY net_name, fault_type
            """).fetchall()

    assert stats.unsat > 0
    assert data["redundant"] > 0
    assert data["denominator"] == data["detected"] + data["undetected"]
    assert data["total_raw_faults"] == data["denominator"] + data["redundant"]
    assert any(
        row["net_name"] == "Y0" and row["fault_type"] == "sa0" for row in redundant_rows
    )


@pytest.mark.integration
def test_stale_redundant_reactivated_when_model_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_const.json"
    if not fixture.exists():
        pytest.skip("tiny_const fixture missing")

    netlist = tmp_path / "output/tiny_const/tiny_const.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top="tiny_const",
        netlist=netlist,
        atpg_overrides={"random_vectors": 0},
    )
    cfg = load_config(cfg_path, "tiny_const")
    model = redundancy_model_id(
        {
            "netlist_hash": "n",
            "cell_lib_hash": "c",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        }
    )
    _run_atpg(cfg, netlist, model)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        conn.execute(
            "UPDATE faults SET redundancy_model_id = 'stale-model' "
            "WHERE status = 'redundant'"
        )
        conn.commit()
        stale = conn.execute(
            "SELECT COUNT(*) FROM faults WHERE status = 'redundant'"
        ).fetchone()[0]
        assert stale > 0

    _run_atpg(cfg, netlist, model)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        stale_left = conn.execute(
            "SELECT COUNT(*) FROM faults WHERE redundancy_model_id = 'stale-model'"
        ).fetchone()[0]

    assert stale_left == 0
    assert data["redundant"] > 0


@pytest.mark.integration
def test_vector_patterns_are_deduplicated_in_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    netlist = tmp_path / "output/tiny_inv/tiny_inv.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(
        tmp_path,
        top="tiny_inv",
        netlist=netlist,
        atpg_overrides={"random_vectors": 16},
    )
    cfg = load_config(cfg_path, "tiny_inv")
    vectors, stats, run_id, _, _ = _run_atpg(
        cfg,
        netlist,
        redundancy_model_id(
            {
                "netlist_hash": "n",
                "cell_lib_hash": "c",
                "collapsing": 0,
                "unsupported_cells": "fail",
                "include_clock_faults": 0,
                "include_reset_faults": 0,
            }
        ),
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT pattern FROM vectors WHERE run_id = ? ORDER BY vector_index",
            (run_id,),
        ).fetchall()

    patterns = [row["pattern"] for row in rows]
    assert len(patterns) == len(set(patterns))
    assert len(patterns) == vectors.count
    assert stats.accepted_vectors == vectors.count


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skip(reason="Sky130: BENCH format not generated for sky130 synth")
def test_cli_ext_vectors_end_to_end_c17(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    pytest.skip("Sky130: BENCH format not generated for sky130 synth")

    ext_test = tmp_path / "c17atpg.test"

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(tmp_path, top="c17", netlist=C17_JSON, threshold=95.0)

    assert (
        main(
            [
                "sim",
                "--top",
                "c17",
                "-c",
                str(cfg_path),
                "--clean",
                "--ext",
                str(ext_test),
            ]
        )
        == 0
    )

    report_path = tmp_path / "output/c17/.faultflow/intermediate/coverage_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["denominator"] > 0
    assert report["run"]["vector_source"].startswith("external:")
    assert report["run"].get("atpg_terminal_reason") in {None, ""}


@pytest.mark.integration
@pytest.mark.slow
def test_cli_progressive_sim_writes_atpg_terminal_to_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    netlist = tmp_path / "output/tiny_inv/tiny_inv.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    _prepare_workspace(tmp_path, monkeypatch)
    cfg_path = _write_cfg(tmp_path, top="tiny_inv", netlist=netlist)

    assert (
        main(
            [
                "sim",
                "--top",
                "tiny_inv",
                "-c",
                str(cfg_path),
                "--clean",
                "--max",
                "3",
                "-t",
                "100",
            ]
        )
        == 0
    )

    report = json.loads(
        (
            tmp_path / "output/tiny_inv/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["run"]["atpg_terminal_reason"] in {
        "COMPLETE",
        "THRESHOLD_MET",
        "MAX_ROUNDS",
        "STALLED",
    }
    assert report["run"]["atpg_rounds"] >= 1
    assert report["summary"]["coverage_percent"] == 100.0


@pytest.mark.integration
@pytest.mark.slow
def test_native_c17_reaches_complete_or_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C17_JSON.exists():
        pytest.skip("c17 netlist missing")

    runner = _runner(
        tmp_path, monkeypatch, top="c17", netlist=C17_JSON, threshold=100.0
    )
    runner.init()
    runner.sim(clean=True, max_rounds=20, target_coverage=100.0)

    report = json.loads(
        (
            tmp_path / "output/c17/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["run"]["vector_source"] == "native_sat_atpg"
    assert report["run"]["atpg_terminal_reason"] in {"COMPLETE", "THRESHOLD_MET"}
    assert float(report["summary"]["coverage_percent"] or 0.0) >= 100.0


@pytest.mark.integration
@pytest.mark.slow
def test_native_c499_improves_coverage_with_clean_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    if not C499_JSON.exists():
        pytest.skip("c499 netlist missing")

    runner = _runner(
        tmp_path,
        monkeypatch,
        top="c499",
        netlist=C499_JSON,
        atpg_overrides={"random_vectors": 0},
        threshold=100.0,
    )
    runner.init()
    runner.sim(clean=True, max_rounds=1, target_coverage=50.0)

    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        after_random_only = summary(conn)

    runner.sim(max_rounds=10, target_coverage=100.0)

    report = json.loads(
        (
            tmp_path / "output/c499/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["run"]["vector_source"] == "native_sat_atpg"
    assert report["run"]["atpg_terminal_reason"] in {
        "COMPLETE",
        "THRESHOLD_MET",
        "STALLED",
        "MAX_ROUNDS",
    }
    assert float(report["summary"]["coverage_percent"] or 0.0) >= float(
        after_random_only["coverage_percent"] or 0.0
    )


@pytest.mark.integration
def test_invalid_max_rounds_raises_from_progressive_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
) -> None:
    fixture = FIXTURES / "tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    netlist = tmp_path / "output/tiny_inv/tiny_inv.json"
    netlist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, netlist)

    runner = _runner(tmp_path, monkeypatch, top="tiny_inv", netlist=netlist)
    runner.init()

    with pytest.raises(RunnerError, match="max_rounds must be >= 1"):
        runner.sim(clean=True, max_rounds=0, target_coverage=100.0)
