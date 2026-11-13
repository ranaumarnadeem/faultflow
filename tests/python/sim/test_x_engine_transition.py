"""Stage 7 of the X engine: [simulation] engine = x grades a combinational
(broadside) transition campaign. A transition is launched only when the good
machine at the fault site is known before and after the edge; given that, the
capture frame's stuck-at is graded definite or possible as a stuck-at is.

tiny_xbit (tests/cpp/fixtures/tiny_xbit.json): y = a & (c | x), n1 = c | x, x
an "x" bit. n1 is never known 0 and never known 1 where it should fall, so
neither of its transitions is ever launched: no credit. c rising shows at y only
as unknown (possible); c falling leaves the good y unknown (nothing). Two-valued,
x is tied to 0 and every one of these is an ordinary detection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import load_config
from faultflow.db import connect, init_schema, summary
from faultflow.runner import Runner

ROOT = Path(__file__).resolve().parents[3]
XBIT = ROOT / "tests/cpp/fixtures/tiny_xbit.json"
C17 = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def core() -> Any:
    loaded = runner_mod._load_core()
    if loaded is None:
        pytest.skip("C++ extension _faultflow_core is required")
    return loaded


def _cfg_text(
    netlist: Path = XBIT, *, engine: str = "x", compaction: str = "reverse"
) -> str:
    return f"""
[design]
netlist = {netlist}
cell_lib = {CELL_LIB}

[fault_model]
model = transition
collapsing = false

[simulation]
unsupported_cells = fail
engine = {engine}

[atpg]
random_vectors = 16
max_rounds = 10
compaction = {compaction}

[report]
threshold = 100.0
"""


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schemas").mkdir(exist_ok=True)
    shutil.copy(
        ROOT / "schemas/coverage.schema.json", tmp_path / "schemas/coverage.schema.json"
    )
    path = tmp_path / "config.ofs"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, top: str
) -> Runner:
    runner = Runner(load_config(_workspace(tmp_path, monkeypatch, text), top))
    runner.init()
    runner.sim(clean=True)
    return runner


def _report(runner: Runner) -> dict[str, Any]:
    return json.loads(runner.cfg.coverage_json_path.read_text(encoding="utf-8"))


def _status_by_site(runner: Runner) -> dict[tuple[str, str], str]:
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT net_name, fault_type, status FROM faults WHERE exclusion = 'none'"
        ).fetchall()
    return {(str(r["net_name"]), str(r["fault_type"])): str(r["status"]) for r in rows}


def test_an_x_transition_campaign_credits_only_known_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = _run(tmp_path, monkeypatch, _cfg_text(), "tiny_xbit")
    statuses = _status_by_site(runner)
    # a and y rise and fall with a known launch, each shown definitely at y.
    for site in (("a", "sa0"), ("a", "sa1"), ("y", "sa0"), ("y", "sa1")):
        assert statuses[site] == "detected", site
    # c rising: launched, but the faulty y = a & x is unknown -- possible.
    assert statuses[("c", "sa0")] == "possible"
    # c falling leaves the good y unknown; n1 never launches: no credit.
    for site in (("c", "sa1"), ("n1", "sa0"), ("n1", "sa1")):
        assert statuses[site] == "undetected", site

    report = _report(runner)
    s = report["summary"]
    assert report["policy"]["engine"] == "x"
    assert s["possible"] == 1
    assert s["test_coverage_percent"] == pytest.approx(
        100.0 * (s["detected"] + 0.5 * s["possible"]) / s["denominator"]
    )
    assert s["detected"] + s["possible"] + s["undetected"] == s["denominator"]
    assert ("c", "sa0") in {
        (f["net_name"], f["fault_type"]) for f in report["possible_faults"]
    }
    # The credit changes the percentages only.
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        none = summary(conn, possible_credit=0.0)
    assert none["test_coverage_percent"] == pytest.approx(
        100.0 * s["detected"] / s["denominator"]
    )


def test_the_two_valued_transition_campaign_reads_the_x_bit_as_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = _run(tmp_path, monkeypatch, _cfg_text(engine="two_valued"), "tiny_xbit")
    statuses = _status_by_site(runner)
    for net in ("a", "c", "n1", "y"):
        for kind in ("sa0", "sa1"):
            assert statuses[(net, kind)] == "detected", (net, kind)
    assert _report(runner)["summary"]["possible"] == 0


def test_transition_compaction_keeps_the_possible_detections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    results = {}
    for compaction in ("none", "reverse"):
        runner = _run(
            tmp_path / compaction,
            monkeypatch,
            _cfg_text(compaction=compaction),
            "tiny_xbit",
        )
        results[compaction] = (_report(runner)["summary"], _status_by_site(runner))
    for key in ("detected", "possible", "test_coverage_percent"):
        assert results["reverse"][0][key] == results["none"][0][key]
    assert results["reverse"][1] == results["none"][1]


def test_with_nothing_unknown_the_x_transition_campaign_is_two_valued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    if not C17.exists():
        pytest.skip("c17 sky130 synthesis is not generated")
    runs = {
        engine: _run(
            tmp_path / engine, monkeypatch, _cfg_text(C17, engine=engine), "c17"
        )
        for engine in ("two_valued", "x")
    }
    assert _status_by_site(runs["x"]) == _status_by_site(runs["two_valued"])
    assert _report(runs["x"])["summary"]["possible"] == 0
