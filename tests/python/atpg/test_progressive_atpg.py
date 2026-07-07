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


def _four_fault_campaign(db: Path) -> tuple[int, dict[int, int]]:
    """A campaign with one fault in each guard-relevant state. Returns
    (campaign_id, {net_id: fault_id})."""
    from db_v3_helpers import insert_campaign, insert_fault_row

    conn = connect(db)
    init_schema(conn)
    cid = insert_campaign(conn)
    rows = [
        # (net_id, status, exclusion, collapsed_into)
        (1, "undetected", "none", None),  # eligible
        (2, "detected", "none", None),  # empirical ground truth
        (3, "excluded", "clock", None),  # outside the denominator
        (4, "undetected", "none", 1),  # collapsed into another site
    ]
    for net_id, status, exclusion, collapsed in rows:
        insert_fault_row(
            conn,
            cid,
            net_id=net_id,
            net_name=f"n{net_id}",
            compiled_net_index=net_id,
            fault_type="SA0",
            status=status,
            exclusion=exclusion,
            collapsed_into=collapsed,
        )
    conn.commit()
    ids = {
        int(r["net_id"]): int(r["id"])
        for r in conn.execute(
            "SELECT id, net_id FROM faults WHERE campaign_id = ?", (cid,)
        )
    }
    conn.close()
    return cid, ids


def test_precertify_redundant_batches_and_guards(tmp_path: Path) -> None:
    """Preflight Phase B pre-certifies canceling-path stems as redundant. It
    must (a) run as ONE batched transaction, not a fresh autocommit connection
    per fault (~51 ms/fault on /mnt/c), and (b) never touch faults that are
    detected (simulation evidence beats the structural claim), excluded, or
    collapsed -- Phase B's id set is derived from net ids over ALL campaign
    faults, so without guards it clobbers all of them and NULLs the detection."""
    from faultflow.runner.progressive_atpg import _precertify_redundant

    db = tmp_path / "faultflow.sqlite"
    _cid, ids = _four_fault_campaign(db)

    marked = _precertify_redundant(str(db), frozenset(ids.values()), "model-x")

    assert marked == 1
    conn = connect(db)
    status_by_net = {
        int(r["net_id"]): str(r["status"])
        for r in conn.execute("SELECT net_id, status FROM faults")
    }
    model_n1 = conn.execute(
        "SELECT redundancy_model_id FROM faults WHERE net_id = 1"
    ).fetchone()[0]
    conn.close()
    assert status_by_net == {
        1: "redundant",
        2: "detected",
        3: "excluded",
        4: "undetected",
    }
    assert model_n1 == "model-x"


def test_mark_fault_redundant_never_overwrites_detected(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The C++ single-fault UNSAT path must carry the same guard: a fault the
    simulator already DETECTED is empirical ground truth, and a late/racing
    UNSAT verdict (e.g. the dynamic fault-drop path detected it mid-wave) must
    not overwrite it -- overwriting also NULLed detected_by_vector, silently
    shrinking coverage. Mirrors mark_fault_protocol_unresolved's guard."""
    import faultflow.runner.runner as runner_mod

    core = runner_mod._load_core()
    db = tmp_path / "faultflow.sqlite"
    _cid, ids = _four_fault_campaign(db)

    core.mark_fault_redundant(str(db), ids[2], "model-x")  # detected fault
    core.mark_fault_redundant(str(db), ids[1], "model-x")  # undetected fault

    conn = connect(db)
    status_by_net = {
        int(r["net_id"]): str(r["status"])
        for r in conn.execute("SELECT net_id, status FROM faults")
    }
    conn.close()
    assert status_by_net[2] == "detected"  # guard held
    assert status_by_net[1] == "redundant"  # active fault still markable


def test_coverage_denominator_excludes_excluded_and_collapsed(tmp_path: Path) -> None:
    from faultflow.runner.progressive_atpg import _coverage_denominator

    db = tmp_path / "faultflow.sqlite"
    cid, _ids = _four_fault_campaign(db)
    # 4 faults: undetected + detected are in scope; excluded + collapsed are not.
    assert _coverage_denominator(str(db), cid) == 2


def test_random_stop_reached_fires_at_threshold(tmp_path: Path) -> None:
    from faultflow.runner.progressive_atpg import _random_stop_reached

    db = tmp_path / "faultflow.sqlite"
    cid, _ids = _four_fault_campaign(db)
    # denominator = 2 in-scope faults, 1 detected -> 50.0% random coverage.
    assert _random_stop_reached(str(db), cid, 2, 50.0) is True  # >= threshold
    assert _random_stop_reached(str(db), cid, 2, 49.9) is True
    assert _random_stop_reached(str(db), cid, 2, 50.1) is False  # not yet
    assert _random_stop_reached(str(db), cid, 2, 0.0) is False  # 0 disables
    assert _random_stop_reached(str(db), cid, 0, 50.0) is False  # empty denom


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
    # fault_model, launch, blackbox_instances, test_mode, and wbr_model are part
    # of the fingerprint; absent -> stuck_at / loc / [] / functional / buffer.
    assert (
        redundancy_model_id(fp)
        == "abc|def|0|fail|0|0|stuck_at|loc|[]|functional|buffer"
    )


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
    root = Path(__file__).resolve().parents[3]
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
    root = Path(__file__).resolve().parents[3]
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
# This test exercises the THRESHOLD_MET terminal branch in isolation. With the
# default per-SAT-pattern fault dropping on, c432 classifies all faults in-round
# and terminates COMPLETE (checked before THRESHOLD_MET), short-circuiting the
# branch under test -- so pin the optimization off here. (M1 coverage-equivalence
# is covered by test_fault_drop_sat_preserves_coverage.)
fault_drop_sat = false

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


@pytest.mark.integration
def test_fault_drop_sat_preserves_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: grading an accepted SAT pattern against all remaining faults keeps the
    fault universe (denominator) identical, never regresses coverage (it can only
    find more real, simulator-verified detections -- on c432 it recovers faults
    the one-fault loop abandons as duplicate patterns), and yields no more
    pre-compaction vectors."""
    import dataclasses

    from faultflow.config import load_config

    root = Path(__file__).resolve().parents[3]
    netlist = root / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")
    monkeypatch.chdir(tmp_path)

    def _run(drop: bool):
        cfg_path = tmp_path / f"c432_{drop}.ofs"
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
fault_drop_sat = {"true" if drop else "false"}
""".strip() + "\n",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path, top="c432")
        cfg = dataclasses.replace(cfg, output_root=tmp_path / f"out_{drop}")
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        _, stats, _, _, _ = _run_atpg(cfg, netlist, _model_id(), target_coverage=100.0)
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            data = summary(conn)
        return stats, data

    on_stats, on = _run(True)
    off_stats, off = _run(False)

    # Same fault universe (pure enumeration).
    assert on["denominator"] == off["denominator"]
    # M1 detects AT LEAST as many real faults: simulating an accepted pattern
    # against all remaining faults recovers detectable faults the one-fault loop
    # abandons as duplicate patterns (rejected at line `if key in seen_patterns`).
    # All detections are simulator-verified, so coverage can only improve, never
    # regress. (On c432 this strictly improves: ~570 vs ~561 detected.)
    assert on["detected"] >= off["detected"]
    assert on["undetected"] <= off["undetected"]
    assert (on["coverage_percent"] or 0.0) >= (off["coverage_percent"] or 0.0)
    # Policy-3 partition holds in both runs (redundant is excluded from the
    # denominator; denominator = detected + undetected).
    for d in (on, off):
        assert d["detected"] + d["undetected"] == d["denominator"]
    assert on["redundant"] == off["redundant"]  # same truly-redundant set
    # M1 win: never MORE pre-compaction vectors than the one-fault path.
    assert on_stats.accepted_vectors <= off_stats.accepted_vectors


@pytest.mark.integration
def test_cone_ordering_preserves_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering faults by cone size only permutes the solve order, so the final
    fault partition is identical: same denominator, detected, undetected, and
    redundant whether the flag is on or off."""
    import dataclasses

    from faultflow.config import load_config

    root = Path(__file__).resolve().parents[3]
    netlist = root / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")
    monkeypatch.chdir(tmp_path)

    def _run(order: bool):
        cfg_path = tmp_path / f"c432_order_{order}.ofs"
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
order_by_cone_size = {"true" if order else "false"}
""".strip() + "\n",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path, top="c432")
        cfg = dataclasses.replace(cfg, output_root=tmp_path / f"out_order_{order}")
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        _run_atpg(cfg, netlist, _model_id(), target_coverage=100.0)
        with connect(cfg.db_path) as conn:
            init_schema(conn)
            return summary(conn)

    on = _run(True)
    off = _run(False)

    for key in ("denominator", "detected", "undetected", "redundant"):
        assert on[key] == off[key], f"{key} differs with cone ordering"


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
def test_stalled_comb_run_keeps_true_undetected_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STALLED combinational run must NOT stamp its undetected stem faults
    protocol_unresolved: that is a scan-protocol concept (the scan pipeline has
    its own q-stem-scoped sweep). The old blanket sweep (a) overwrote the true
    per-fault timeout/unknown reason in the coverage report and (b) made every
    grading path with skip_protocol_unresolved=true skip these faults forever,
    so a resumed campaign could never re-target them."""
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

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        marked = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM faults WHERE protocol_unresolved = 1"
            ).fetchone()["n"]
        )
        undetected = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM faults WHERE status = 'undetected'"
            ).fetchone()["n"]
        )
    assert undetected > 0  # the faults are still there, still re-targetable
    assert marked == 0  # ...and none were mislabeled protocol_unresolved


@pytest.mark.unit
def test_broken_worker_pool_fails_loudly_not_silent_unknowns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a parallel SAT worker process dies (OOM-killed -- the observed
    incremental-SAT memory-blowup scenario), the run must abort with an
    actionable RunnerError. The old blanket `except Exception` swallowed
    BrokenProcessPool with one log.warning, defaulted the whole wave to
    UNKNOWN, and -- because the dead pool is never recreated -- every later
    wave of every later round did the same, so the run 'completed' with a
    garbage coverage number instead of failing."""
    cfg, netlist = _tiny_inv_cfg(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        .replace("random_vectors = 8", "random_vectors = 0")
        .replace("sat_conflict_limit", "workers = 2\nsat_conflict_limit"),
        encoding="utf-8",
    )
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    from faultflow.config import load_config
    from faultflow.runner import RunnerError

    cfg = load_config(cfg_path, top="tiny_inv")
    assert cfg.atpg.workers == 2

    def dead_pool_map(_self: object, *_args: Any, **_kwargs: Any) -> Any:
        raise BrokenProcessPool("A child process terminated abruptly")

    monkeypatch.setattr(ProcessPoolExecutor, "map", dead_pool_map)

    with pytest.raises(RunnerError, match="worker"):
        _run_atpg(cfg, netlist, _model_id(), max_rounds=3, target_coverage=100.0)


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
    root = Path(__file__).resolve().parents[3]
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
cell_lib = {Path(__file__).resolve().parents[3] / "cells/sky130/sky130_fd_sc_hd.json"}

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
