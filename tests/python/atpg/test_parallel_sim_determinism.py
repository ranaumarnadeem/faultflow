"""End-to-end determinism of parallel fault grading (simulation.sim_threads).

The C++ unit tests prove the grading core is bit-identical across thread counts.
These tests prove the SAME holds through the full Python plumbing: config parse
-> runner resolve -> pybind binding (GIL released around the C++ grade) -> the
threaded core. Coverage and the detected fault set must not depend on
sim_threads; only wall-clock should.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from faultflow.config import load_config, resolve_sim_threads
from faultflow.db import connect, init_schema, summary
from faultflow.runner.progressive_atpg import (
    redundancy_model_id,
    run_progressive_native_atpg,
    run_progressive_transition_atpg,
)
from campaign_fixtures import campaign_id_for_cfg

ROOT = Path(__file__).resolve().parents[3]


def _model_id() -> str:
    return redundancy_model_id(
        {
            "netlist_hash": "x",
            "cell_lib_hash": "y",
            "collapsing": 0,
            "unsupported_cells": "fail",
            "include_clock_faults": 0,
            "include_reset_faults": 0,
        }
    )


def _run_for_threads(
    tmp_path: Path,
    top: str,
    netlist: Path,
    sim_threads: int,
    *,
    transition: bool = False,
) -> dict:
    """Run the full ATPG with a given sim_threads on its own output dir, returning
    a thread-count-independent fingerprint of the result. Covers both the stuck-at
    (run_progressive_native_atpg) and transition (run_progressive_transition_atpg)
    grading paths so each is proven deterministic through the Python plumbing."""
    model_lines = "model = transition\n" if transition else ""
    cfg_path = tmp_path / f"{top}_t{sim_threads}.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {ROOT / "cells/sky130/sky130_fd_sc_hd.json"}

[fault_model]
{model_lines}collapsing = false

[simulation]
unsupported_cells = fail
sim_threads = {sim_threads}

[atpg]
random_vectors = 64
max_rounds = 20
sat_timeout_seconds = 10

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, top=top)
    cfg = dataclasses.replace(cfg, output_root=tmp_path / f"out_{top}_t{sim_threads}")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    campaign_id = campaign_id_for_cfg(cfg, netlist)
    run = run_progressive_transition_atpg if transition else run_progressive_native_atpg
    run(cfg, netlist, _model_id(), campaign_id=campaign_id, target_coverage=100.0)

    with connect(cfg.db_path) as conn:
        init_schema(conn)
        data = summary(conn)
        # (net_name, fault_type, first-detection vector) of every detected fault:
        # the vector index is deterministic given the (seeded) vector order, so it
        # must be identical across thread counts too.
        detected = sorted(
            (row["net_name"], row["fault_type"], row["detected_by_vector"])
            for row in conn.execute(
                "SELECT net_name, fault_type, detected_by_vector "
                "FROM faults WHERE status = 'detected'"
            )
        )
    return {
        "denominator": data["denominator"],
        "detected": data["detected"],
        "undetected": data["undetected"],
        "coverage_percent": data["coverage_percent"],
        "detected_set": detected,
    }


def test_resolve_sim_threads_contract() -> None:
    import os

    assert resolve_sim_threads(1) == 1
    assert resolve_sim_threads(4) == 4
    auto = resolve_sim_threads(0)
    assert auto == max(1, (os.cpu_count() or 1) - 2)
    assert auto >= 1


@pytest.mark.unit
def test_sim_threads_plumbing_is_inert_on_tiny_inv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads plumbing must run cleanly end-to-end and not change the result,
    even where the fault list is far smaller than one batch (threads clamp)."""
    fixture = ROOT / "tests/cpp/fixtures/tiny_inv.json"
    if not fixture.exists():
        pytest.skip("tiny_inv fixture missing")
    monkeypatch.chdir(tmp_path)

    serial = _run_for_threads(tmp_path, "tiny_inv", fixture, 1)
    parallel = _run_for_threads(tmp_path, "tiny_inv", fixture, 8)
    assert parallel == serial


@pytest.mark.integration
def test_sim_threads_determinism_on_c432(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """c432 has enough faults for many 63-fault batches per vector, so this
    genuinely exercises multi-slice parallel grading. Coverage and the detected
    set must be byte-identical for sim_threads in {1, 2, 4, 8}."""
    netlist = ROOT / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")
    monkeypatch.chdir(tmp_path)

    serial = _run_for_threads(tmp_path, "c432", netlist, 1)
    assert serial["detected"] > 0
    for threads in (2, 4, 8):
        result = _run_for_threads(tmp_path, "c432", netlist, threads)
        assert result == serial, f"sim_threads={threads} diverged from serial"


@pytest.mark.integration
def test_sim_threads_determinism_transition_on_c432(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-frame transition grading (run_progressive_transition_atpg) must be just
    as thread-count-independent as the stuck-at path."""
    netlist = ROOT / "tests/benchmarks/iscas85/synth_sky130/c432.json"
    if not netlist.exists():
        pytest.skip("c432 netlist missing")
    monkeypatch.chdir(tmp_path)

    serial = _run_for_threads(tmp_path, "c432", netlist, 1, transition=True)
    assert serial["detected"] > 0
    parallel = _run_for_threads(tmp_path, "c432", netlist, 4, transition=True)
    assert parallel == serial
