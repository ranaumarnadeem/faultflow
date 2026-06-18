from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from faultflow.db import connect, init_schema, summary
from faultflow.runner.progressive_atpg import (
    ATPGRANDOM_SEED,
    DEFAULT_MAX_ATPG_ROUNDS,
    _append_unique_vectors,
    pattern_key,
    redundancy_model_id,
    run_progressive_native_atpg,
)
from campaign_fixtures import campaign_id_for_cfg


def test_pattern_key_matches_cpp_order() -> None:
    vector = {"A": True, "B": False}
    assert pattern_key(vector, ["A", "B"]) == "10"
    assert pattern_key(vector, ["B", "A"]) == "01"


def test_redundancy_model_id_is_stable() -> None:
    fp = {
        "netlist_hash": "abc",
        "cell_lib_hash": "def",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
    }
    # fault_model and launch are part of the redundancy fingerprint; absent ->
    # stuck_at / loc.
    assert redundancy_model_id(fp) == "abc|def|0|fail|0|0|stuck_at|loc"


def test_summary_includes_redundant(tmp_path: Path) -> None:
    db = tmp_path / "faultflow.sqlite"
    with connect(db) as conn:
        init_schema(conn)
        from db_v3_helpers import insert_campaign, insert_fault_row

        campaign_id = insert_campaign(conn)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="n1",
            compiled_net_index=0,
            fault_type="sa0",
            status="detected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=2,
            net_name="n2",
            compiled_net_index=1,
            fault_type="sa1",
            status="undetected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=3,
            net_name="n3",
            compiled_net_index=2,
            fault_type="sa0",
            status="redundant",
        )
        data = summary(conn, campaign_id=campaign_id)
    assert data["detected"] == 1
    assert data["undetected"] == 1
    assert data["redundant"] == 1
    assert data["denominator"] == 2


def test_duplicate_suppression_drops_repeated_patterns() -> None:
    vectors: list[dict[str, bool]] = []
    seen: set[str] = set()
    batch = [{"A": True, "B": False}, {"A": True, "B": False}, {"A": False, "B": True}]
    accepted = _append_unique_vectors(vectors, seen, ["A", "B"], batch)
    assert len(accepted) == 2
    assert len(vectors) == 2
    assert seen == {"10", "01"}


def _tiny_inv_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[2]
    fixture = root / "tests/cpp/fixtures/tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {fixture}
cell_lib = {root / "cells/sky130/sky130_fd_sc_hd.json"}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 8
sat_conflict_limit = 100000
max_rounds = 20
sat_timeout_seconds = 10

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from faultflow.config import load_config

    cfg = load_config(cfg_path, top="tiny_inv")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, cfg.output_dir / "tiny_inv.json")
    return cfg, cfg.output_dir / "tiny_inv.json"


def _model_id() -> str:
    fp = {
        "netlist_hash": "x",
        "cell_lib_hash": "y",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
    }
    return redundancy_model_id(fp)


def _run_atpg(cfg, netlist, model_id: str, **kwargs: object):
    campaign_id = campaign_id_for_cfg(cfg, netlist)
    return run_progressive_native_atpg(
        cfg, netlist, model_id, campaign_id=campaign_id, **kwargs
    )


@pytest.mark.unit
def test_progressive_native_atpg_tiny_inv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    vectors, stats, run_id, _, _ = _run_atpg(cfg, netlist, _model_id())
    assert vectors.count > 0
    assert stats.terminal_reason in {
        "COMPLETE",
        "THRESHOLD_MET",
        "MAX_ROUNDS",
        "STALLED",
    }
    assert stats.rounds >= 1
    assert run_id >= 1

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        run = conn.execute(
            "SELECT atpg_terminal_reason, atpg_rounds FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert data["denominator"] > 0
    assert run is not None
    assert run["atpg_terminal_reason"] == stats.terminal_reason


@pytest.mark.integration
def test_threshold_met_stops_before_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    netlist = root / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")

    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {root / "cells/sky130/sky130_fd_sc_hd.json"}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
random_vectors = 64
max_rounds = 20
sat_timeout_seconds = 10

[report]
threshold = 95.0
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    from faultflow.config import load_config

    cfg = load_config(cfg_path, top="c432")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    _, stats, _, _, _ = _run_atpg(cfg, netlist, _model_id(), target_coverage=95.0)
    assert stats.terminal_reason == "THRESHOLD_MET"
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
    assert data["undetected"] > 0
    assert float(data["coverage_percent"] or 0.0) >= 95.0


@pytest.mark.unit
def test_max_rounds_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    _, stats, _, _, _ = _run_atpg(
        cfg, netlist, _model_id(), max_rounds=1, target_coverage=100.0
    )
    assert stats.rounds == 1
    assert stats.terminal_reason in {"COMPLETE", "THRESHOLD_MET", "MAX_ROUNDS"}


@pytest.mark.unit
def test_verify_fault_candidate_rejects_non_detecting_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)

    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")

    core.ensure_faults_enumerated(
        str(netlist),
        str(cfg.cell_lib),
        str(cfg.db_path),
        campaign_id_for_cfg(cfg, netlist),
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        cfg.simulation.unsupported_cells,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        row = conn.execute("""
            SELECT id
            FROM faults
            WHERE net_name = 'Y' AND fault_type = 'sa1' AND status = 'undetected'
            """).fetchone()
    assert row is not None
    fault_id = int(row["id"])

    bad_vector = {"A": False}
    assert not core.verify_fault_candidate(
        str(netlist),
        str(cfg.cell_lib),
        str(cfg.db_path),
        fault_id,
        bad_vector,
        cfg.simulation.unsupported_cells,
    )


@pytest.mark.unit
def test_rejected_sat_candidate_increments_stats_and_keeps_fault_undetected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "random_vectors = 8", "random_vectors = 0"
        ),
        encoding="utf-8",
    )
    from faultflow.config import load_config

    cfg = load_config(cfg_path, top="tiny_inv")

    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")

    bad_vector = {"A": False}
    core.ensure_faults_enumerated(
        str(netlist),
        str(cfg.cell_lib),
        str(cfg.db_path),
        campaign_id_for_cfg(cfg, netlist),
        cfg.fault_model.include_clock_faults,
        cfg.fault_model.include_reset_faults,
        cfg.fault_model.collapsing,
        cfg.simulation.unsupported_cells,
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        target_row = conn.execute("""
            SELECT id
            FROM faults
            WHERE net_name = 'Y' AND fault_type = 'sa1' AND status = 'undetected'
            """).fetchone()
    assert target_row is not None
    target_fault_id = int(target_row["id"])

    def rejectable_sat_solve(*args: Any, **kwargs: Any) -> dict[str, object]:
        del kwargs
        fault_id = int(args[3])
        if fault_id == target_fault_id:
            return {"result": "SAT", "vector": bad_vector}
        return {"result": "TIMEOUT", "vector": {}}

    monkeypatch.setattr(core, "solve_fault_atpg", rejectable_sat_solve)
    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])

    _, stats, run_id, _, _ = _run_atpg(
        cfg, netlist, _model_id(), max_rounds=1, target_coverage=100.0
    )

    assert stats.sat == 1
    assert stats.rejected_candidates == 1

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        fault = conn.execute(
            "SELECT status FROM faults WHERE id = ?",
            (target_fault_id,),
        ).fetchone()
        run = conn.execute(
            """
            SELECT atpg_rejected_candidates
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    assert fault is not None
    assert fault["status"] == "undetected"
    assert run is not None
    assert int(run["atpg_rejected_candidates"]) == 1


@pytest.mark.unit
def test_stalled_when_sat_only_returns_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "random_vectors = 8", "random_vectors = 0"
        ),
        encoding="utf-8",
    )
    from faultflow.config import load_config

    cfg = load_config(cfg_path, top="tiny_inv")

    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    assert core is not None

    def timeout_solve(*args: Any, **kwargs: Any) -> dict[str, object]:
        del args, kwargs
        return {"result": "TIMEOUT", "vector": {}}

    monkeypatch.setattr(core, "solve_fault_atpg", timeout_solve)
    monkeypatch.setattr(core, "atpg_random_vectors", lambda *_a, **_k: [])

    _, stats, _, _, _ = _run_atpg(
        cfg, netlist, _model_id(), max_rounds=3, target_coverage=100.0
    )
    assert stats.terminal_reason == "STALLED"
    assert stats.timeout > 0

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM faults WHERE status = 'undetected'"
        ).fetchone()
    assert row is not None
    assert int(row["n"]) > 0


@pytest.mark.unit
def test_resume_preserves_detected_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    _, first_stats, _, _, _ = _run_atpg(
        cfg, netlist, _model_id(), max_rounds=1, target_coverage=100.0
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        before = summary(conn)
        fault_count = conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0]

    _, second_stats, _, _, _ = _run_atpg(
        cfg, netlist, _model_id(), max_rounds=20, target_coverage=100.0
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        after = summary(conn)
        fault_count_after = conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0]

    assert fault_count_after == fault_count
    assert after["detected"] >= before["detected"]
    assert second_stats.rounds >= 1
    assert first_stats.rounds == 1


@pytest.mark.unit
def test_coverage_report_includes_atpg_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    _, stats, run_id, _, _ = _run_atpg(cfg, netlist, _model_id())

    from faultflow.reporter import write_reports

    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        root / "schemas/coverage.schema.json", schema_dir / "coverage.schema.json"
    )

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        _, _, report = write_reports(conn, cfg)

    run = report["run"]
    assert run["id"] == run_id
    assert run["atpg_terminal_reason"] == stats.terminal_reason
    assert run["atpg_rounds"] == stats.rounds
    assert run["atpg_sat"] == stats.sat
    assert run["atpg_unsat"] == stats.unsat


def test_constants_locked() -> None:
    assert DEFAULT_MAX_ATPG_ROUNDS == 20
    assert ATPGRANDOM_SEED == 0x5EED5EED


def test_cli_sim_passes_max_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        """
[design]
netlist = missing.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[simulation]
unsupported_cells = fail

[atpg]
max_rounds = 20

[report]
threshold = 95.0
""".strip() + "\n",
        encoding="utf-8",
    )
    (tmp_path / "missing.json").write_text("{}", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_sim(
        self: object,
        *,
        purge: bool = False,
        clean: bool = False,
        verify: bool | None = None,
        ext: Path | None = None,
        max_rounds: int | None = None,
        target_coverage: float | None = None,
        scan: bool = False,
    ) -> str:
        del self, purge, clean, verify, ext, scan
        captured["max_rounds"] = max_rounds
        captured["target_coverage"] = target_coverage
        return "sim complete"

    monkeypatch.setattr("faultflow.runner.Runner.sim", fake_sim)

    assert (
        main(["sim", "--top", "demo", "-c", str(cfg_path), "--max", "7", "-t", "88.5"])
        == 0
    )
    assert captured["max_rounds"] == 7
    assert captured["target_coverage"] == 88.5


def test_cli_sim_rejects_invalid_max_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        "[design]\nnetlist = x.json\ncell_lib = cells/sky130/sky130_fd_sc_hd.json\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        main(["sim", "--top", "demo", "-c", str(cfg_path), "--max", "0"])
    with pytest.raises(SystemExit):
        main(["sim", "--top", "demo", "-c", str(cfg_path), "-t", "0"])
    with pytest.raises(SystemExit):
        main(["sim", "--top", "demo", "-c", str(cfg_path), "-t", "101"])


def test_runner_ext_skips_progressive_atpg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.atpg import VectorSet
    from faultflow.config import load_config
    from faultflow.runner import Runner

    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    netlist = tmp_path / "demo.json"
    netlist.write_text(
        '{"creator":"yosys","modules":{"demo":{"attributes":{"top":1},'
        '"ports":{"a":{"direction":"input","bits":[2]},'
        '"y":{"direction":"output","bits":[3]}},"cells":{}}}}',
        encoding="utf-8",
    )
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {Path(__file__).resolve().parents[2] / "cells/sky130/sky130_fd_sc_hd.json"}

[simulation]
unsupported_cells = fail
""".strip() + "\n",
        encoding="utf-8",
    )
    ext = tmp_path / "vectors.test"
    ext.write_text("vector\na=0\n", encoding="utf-8")
    bench = tmp_path / "vectors.bench"
    bench.write_text("INPUT(a)\nOUTPUT(y)\ny = BUFF(a)\n", encoding="utf-8")

    progressive_called = False

    def fake_progressive(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        nonlocal progressive_called
        progressive_called = True
        raise AssertionError("progressive ATPG should not run with --ext")

    monkeypatch.setattr(
        "faultflow.runner.progressive_atpg.run_progressive_native_atpg",
        fake_progressive,
    )
    monkeypatch.setattr(
        Runner,
        "_fingerprint",
        lambda self, _netlist: {
            "netlist_hash": "n",
            "cell_lib_hash": "c",
            "config_hash": "g",
            "template_hash": "t",
            "yosys_version": "y",
            "faultflow_version": "v",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        },
    )
    monkeypatch.setattr(
        Runner, "_ensure_campaign", lambda self, conn, fp, scan=False: 1
    )
    monkeypatch.setattr(
        Runner,
        "_simulate_with_core",
        lambda self, _netlist, vectors, source, campaign_id: {"run_id": 1},
    )
    monkeypatch.setattr(
        Runner,
        "_external_vectors",
        lambda self, ext_path, _netlist: (
            VectorSet("external", ["a"], [{"a": False}]),
            bench,
        ),
    )
    monkeypatch.setattr(
        "faultflow.runner.runner.write_reports",
        lambda conn, cfg, scan_context=None, campaign_id=None: (
            cfg.coverage_json_path,
            cfg.coverage_report_path,
            {"summary": {"coverage_percent": 50.0}},
        ),
    )

    cfg = load_config(cfg_path, "demo")
    cfg.ensure_workspace()
    cfg.db_path.write_bytes(b"")

    Runner(cfg).sim(ext=ext)
    assert not progressive_called
