"""Stage 2b of the X engine: [simulation] engine = x grades a combinational
campaign on the X engine -- an x/z bit of the netlist unknown, a fault it shows
only through that unknown 'possible', credited [fault_model] possible_credit --
and the report counts it.

tiny_xbit (tests/cpp/fixtures/tiny_xbit.json): y = a & (c | x), x an "x" bit.
Stuck at 0, c changes y only where x = 0, so no vector detects it definitely:
the X engine sees y unknown where the good y is 1 (possible). Two-valued, x is
tied to 0 and c stuck at 0 is an ordinary detection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import ConfigError, load_config
from faultflow.db import connect, init_schema, summary
from faultflow.runner import Runner
from faultflow.runner.runner import FingerprintMismatchError, RunnerError

ROOT = Path(__file__).resolve().parents[3]
XBIT = ROOT / "tests/cpp/fixtures/tiny_xbit.json"
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.fixture(scope="module")
def core() -> Any:
    loaded = runner_mod._load_core()
    if loaded is None:
        pytest.skip("C++ extension _faultflow_core is required")
    return loaded


def _cfg_text(
    *, engine: str = "x", credit: str = "", compaction: str = "reverse"
) -> str:
    return f"""
[design]
netlist = {XBIT}
cell_lib = {CELL_LIB}

[fault_model]
collapsing = false
{credit}

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


def _report(runner: Runner) -> dict[str, Any]:
    return json.loads(runner.cfg.coverage_json_path.read_text(encoding="utf-8"))


def _status_by_site(runner: Runner) -> dict[tuple[str, str], str]:
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT net_name, fault_type, status FROM faults WHERE exclusion = 'none'"
        ).fetchall()
    return {(str(r["net_name"]), str(r["fault_type"])): str(r["status"]) for r in rows}


# --- config -----------------------------------------------------------------


def test_the_engine_defaults_to_two_valued(tmp_path: Path) -> None:
    path = tmp_path / "c.ofs"
    path.write_text("[design]\nnetlist = n.json\ncell_lib = c.json\n", encoding="utf-8")
    cfg = load_config(path, "top")
    assert cfg.simulation.engine == "two_valued"
    assert cfg.simulation.graph_policy == "fail"
    assert cfg.fault_model.possible_credit == 50.0


def test_engine_x_asks_the_core_for_an_x_mode_graph(tmp_path: Path) -> None:
    path = tmp_path / "c.ofs"
    path.write_text(
        "[design]\nnetlist = n.json\ncell_lib = c.json\n"
        "[simulation]\nengine = x\nunsupported_cells = blackbox\n"
        "[fault_model]\npossible_credit = 25\n",
        encoding="utf-8",
    )
    cfg = load_config(path, "top")
    assert cfg.simulation.graph_policy == "blackbox+x"
    assert cfg.fault_model.possible_credit == 25.0


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[simulation]\nengine = three\n", "engine"),
        ("[fault_model]\npossible_credit = 120\n", "possible_credit"),
        ("[fault_model]\npossible_credit = half\n", "possible_credit"),
    ],
)
def test_bad_engine_settings_are_refused(tmp_path: Path, body: str, match: str) -> None:
    path = tmp_path / "c.ofs"
    path.write_text(
        "[design]\nnetlist = n.json\ncell_lib = c.json\n" + body, encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=match):
        load_config(path, "top")


# --- a campaign ---------------------------------------------------------------


def test_an_x_engine_campaign_credits_what_it_only_possibly_detects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = Runner(
        load_config(_workspace(tmp_path, monkeypatch, _cfg_text()), "tiny_xbit")
    )
    runner.init()
    runner.sim(clean=True)
    report = _report(runner)
    s = report["summary"]
    assert report["policy"]["engine"] == "x"
    assert report["policy"]["possible_credit_percent"] == 50.0
    assert s["possible"] >= 1
    assert s["possible_credit_percent"] == 50.0
    credited = s["detected"] + 0.5 * s["possible"]
    assert s["test_coverage_percent"] == pytest.approx(
        100.0 * credited / s["denominator"]
    )
    assert s["detected"] + s["possible"] + s["undetected"] == s["denominator"]

    statuses = _status_by_site(runner)
    assert statuses[("c", "sa0")] == "possible"
    assert statuses[("a", "sa0")] == "detected"
    listed = {(f["net_name"], f["fault_type"]) for f in report["possible_faults"]}
    assert ("c", "sa0") in listed
    assert all(
        f["net_name"] != "c" or f["fault_type"] != "sa0"
        for f in report["undetected_faults"]
    )
    text = runner.cfg.coverage_report_path.read_text(encoding="utf-8")
    assert "possible:" in text and "credited 50% each" in text

    # The credit changes the percentages only, never a status.
    with connect(runner.cfg.db_path) as conn:
        init_schema(conn)
        none = summary(conn, possible_credit=0.0)
        full = summary(conn, possible_credit=100.0)
    assert none["test_coverage_percent"] == pytest.approx(
        100.0 * s["detected"] / s["denominator"]
    )
    assert full["test_coverage_percent"] == pytest.approx(
        100.0 * (s["detected"] + s["possible"]) / s["denominator"]
    )
    assert _status_by_site(runner) == statuses


def test_compaction_keeps_the_possible_detections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    results = {}
    for compaction in ("none", "reverse"):
        runner = Runner(
            load_config(
                _workspace(
                    tmp_path / compaction, monkeypatch, _cfg_text(compaction=compaction)
                ),
                "tiny_xbit",
            )
        )
        runner.init()
        runner.sim(clean=True)
        results[compaction] = _report(runner)["summary"]
    for key in ("detected", "possible", "test_coverage_percent"):
        assert results["reverse"][key] == results["none"][key]


def test_the_two_valued_engine_reads_the_x_bit_as_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = Runner(
        load_config(
            _workspace(tmp_path, monkeypatch, _cfg_text(engine="two_valued")),
            "tiny_xbit",
        )
    )
    runner.init()
    runner.sim(clean=True)
    report = _report(runner)
    assert report["summary"]["possible"] == 0
    assert "engine" not in report["policy"]
    assert "possible_faults" not in report
    assert _status_by_site(runner)[("c", "sa0")] == "detected"
    assert "possible:" not in runner.cfg.coverage_report_path.read_text(
        encoding="utf-8"
    )


def test_a_campaign_does_not_resume_across_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    path = _workspace(tmp_path, monkeypatch, _cfg_text(engine="two_valued"))
    two = Runner(load_config(path, "tiny_xbit"))
    two.init()
    two.sim(clean=True)
    path.write_text(_cfg_text(engine="x").strip() + "\n", encoding="utf-8")
    with pytest.raises(FingerprintMismatchError, match="config_hash"):
        Runner(load_config(path, "tiny_xbit")).sim()


def test_the_x_engine_refuses_scan_and_the_iverilog_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    runner = Runner(
        load_config(_workspace(tmp_path, monkeypatch, _cfg_text()), "tiny_xbit")
    )
    runner.init()
    with pytest.raises(RunnerError, match="scan"):
        runner.sim(scan=True)
    with pytest.raises(RunnerError, match="known outputs"):
        runner.sim(verify=True)


# --- the bindings ---------------------------------------------------------------


def test_external_vectors_are_graded_on_the_x_engine(tmp_path: Path, core: Any) -> None:

    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO campaigns(campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, collapsing, "
            "unsupported_cells, include_clock_faults, include_reset_faults) VALUES "
            "('comb', 'tiny_xbit', 'n', 'c', 'k', 't', 'y', 'v', 0, 'fail', 0, 0)"
        )
        conn.commit()
    result = core.simulate_to_db_x(
        str(XBIT),
        str(CELL_LIB),
        str(db),
        1,
        [{"a": True, "c": True}],
        ["a", "c"],
        "external:test",
        possible_credit=0.5,
    )
    assert result["possible"] >= 1
    credited = result["detected"] + 0.5 * result["possible"]
    assert result["coverage_percent"] == pytest.approx(
        100.0 * credited / result["denominator"]
    )
    with connect(db) as conn:
        init_schema(conn)
        statuses = {
            (str(r["net_name"]), str(r["fault_type"])): str(r["status"])
            for r in conn.execute("SELECT net_name, fault_type, status FROM faults")
        }
    assert statuses[("c", "sa0")] == "possible"
    assert statuses[("a", "sa0")] == "detected"
