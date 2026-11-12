"""Stage 4 of the X engine: combinational loops. A two-valued run refuses a netlist
with one, naming its cells -- it used to simulate it silently wrong. The X engine
settles each loop from its last values (a loop of gates that holds a value keeps
it), and grades it.

tiny_loop_sr (tests/cpp/fixtures): q = nand(s_n, qb), qb = nand(r_n, q), y = q & a."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import load_config
from faultflow.runner import Runner

ROOT = Path(__file__).resolve().parents[3]
SR = ROOT / "tests/cpp/fixtures/tiny_loop_sr.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def core() -> Any:
    loaded = runner_mod._load_core()
    if loaded is None:
        pytest.skip("C++ extension _faultflow_core is required")
    return loaded


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str) -> Runner:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schemas").mkdir(exist_ok=True)
    shutil.copy(ROOT / "schemas/coverage.schema.json", tmp_path / "schemas")
    path = tmp_path / "config.ofs"
    path.write_text(
        f"""
[design]
netlist = {SR}
cell_lib = {CELL_LIB}

[fault_model]
collapsing = false

[simulation]
engine = {engine}

[atpg]
random_vectors = 32
max_rounds = 5

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    runner = Runner(load_config(path, "tiny_loop_sr"))
    runner.init()
    return runner


def test_a_two_valued_run_refuses_a_combinational_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = _runner(tmp_path, monkeypatch, "two_valued")
    with pytest.raises(Exception, match="combinational loop through cells u1, u2"):
        runner.sim(clean=True)


def test_the_x_engine_grades_a_gate_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = _runner(tmp_path, monkeypatch, "x")
    runner.sim(clean=True)
    report = json.loads(runner.cfg.coverage_json_path.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["detected"] > 0
    # Nothing is proven redundant through the loop.
    assert summary["redundant"] == 0
    assert (
        summary["detected"] + summary["possible"] + summary["undetected"]
        == summary["denominator"]
    )
