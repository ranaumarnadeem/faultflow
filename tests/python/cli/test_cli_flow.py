import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from faultflow.atpg import VectorSet
from faultflow.cli import main
from faultflow.config import load_config
from faultflow.db import (
    connect,
    init_schema,
    record_reconvergent_stems,
    record_sat_outcomes,
)
from db_v3_helpers import insert_campaign, insert_fault_row, insert_run
from faultflow.reporter import CoverageError, write_reports
from faultflow.runner import Runner, RunnerError
import faultflow.runner.runner as runner_mod


def _config(path: Path) -> None:
    path.write_text(
        """
[design]
netlist = missing.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
output = patterns.test

[report]
threshold = 95.0
""".strip() + "\n",
        encoding="utf-8",
    )


def test_init_requires_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)

    with pytest.raises(SystemExit):
        main(["init", "-c", str(cfg)])


def test_init_creates_top_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    (tmp_path / "missing.json").write_text("{}", encoding="utf-8")

    assert main(["init", "--top", "demo", "-c", str(cfg)]) == 0
    assert (tmp_path / "output" / "demo" / ".faultflow" / "faultflow.sqlite").exists()


def test_sim_on_sequential_design_gives_clean_scan_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    require_cpp_core: None,
) -> None:
    """Plain (non-scan) `sim` on a design with flip-flops must fail with a clean,
    actionable CLI error pointing at --scan (exit 2), not leak the C++ engine's
    bare `progressive native ATPG is combinational-only` RuntimeError traceback --
    forgetting --scan on a sequential design is a very common mistake."""
    monkeypatch.chdir(tmp_path)
    repo = Path(__file__).resolve().parents[3]
    fixture = repo / "tests" / "cpp" / "fixtures" / "tiny_dff.json"
    cell_lib = repo / "cells" / "sky130" / "sky130_fd_sc_hd.json"
    cfg = tmp_path / "config.ofs"
    cfg.write_text(
        f"""
[design]
netlist = {fixture}
cell_lib = {cell_lib}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
compaction = none
""".strip() + "\n",
        encoding="utf-8",
    )

    assert main(["init", "--top", "tiny_dff", "-c", str(cfg)]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        main(["sim", "--top", "tiny_dff", "-c", str(cfg)])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "sequential elements" in err
    assert "--scan" in err
    assert "Traceback" not in err
    assert "combinational-only" not in err  # the raw engine message is not leaked


def test_status_command_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status` is wired via argparse -> FlowService.status -> Runner.status. A
    dest= typo on --scan or the `status` subparser itself would go undetected
    without an actual `main([...])` invocation (FlowService.status is otherwise
    only exercised directly, never through the CLI)."""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    (tmp_path / "missing.json").write_text("{}", encoding="utf-8")

    assert main(["init", "--top", "demo", "-c", str(cfg)]) == 0
    capsys.readouterr()

    # No campaign run yet -> status reports coverage=n/a, but must still exit 0
    # and correctly thread the --scan flag through to FlowService/Runner (the
    # printed scan_mode reflects the actual arg, proving argparse wiring works).
    assert main(["status", "--top", "demo", "-c", str(cfg)]) == 0
    assert "scan_mode=false" in capsys.readouterr().out

    assert main(["status", "--top", "demo", "-c", str(cfg), "--scan"]) == 0
    assert "scan_mode=true" in capsys.readouterr().out


def test_init_rejects_fingerprint_mismatch_by_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    (tmp_path / "missing.json").write_text("{}", encoding="utf-8")

    assert main(["init", "--top", "demo", "-c", str(cfg)]) == 0
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "unsupported_cells = fail", "unsupported_cells = blackbox"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(["init", "--top", "demo", "-c", str(cfg)])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "changed" in err
    assert "--clean" in err


def test_sim_model_override_selects_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    import faultflow.cli as cli_mod

    captured: dict[str, str] = {}

    def fake_run_atpg(_self: object, cfg: object, **_kwargs: object) -> object:
        captured["model"] = cfg.fault_model.model  # type: ignore[attr-defined]
        return SimpleNamespace(message="ok")

    monkeypatch.setattr(cli_mod.FlowService, "run_atpg", fake_run_atpg)

    assert (
        main(["sim", "--top", "demo", "-c", str(cfg_path), "--model", "transition"])
        == 0
    )
    assert captured["model"] == "transition"

    # No flag -> config default (stuck_at).
    assert main(["sim", "--top", "demo", "-c", str(cfg_path)]) == 0
    assert captured["model"] == "stuck_at"


def test_sim_model_transition_rejects_collapsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "collapsing = false", "collapsing = true"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(["sim", "--top", "demo", "-c", str(cfg_path), "--model", "transition"])
    assert exc.value.code == 2


def test_sim_requires_cpp_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    runner = Runner(cfg)
    monkeypatch.setattr(runner_mod, "_load_core", lambda: None)

    with pytest.raises(RunnerError, match="_faultflow_core is required"):
        runner._simulate_with_core(
            Path("missing.json"),
            VectorSet(source="vectors.test", input_order=[], vectors=[]),
            "vectors.test",
            1,
        )


def test_clean_workspace_removes_internal_state_keeps_deliverables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    runner = Runner(load_config(cfg_path, "demo"))
    out = tmp_path / "output" / "demo"
    workspace = out / ".faultflow"
    workspace.mkdir(parents=True)
    keep = out / "coverage.rpt"
    keep.write_text("deliverable", encoding="utf-8")
    db_files = [
        workspace / "faultflow.sqlite",
        workspace / "faultflow.sqlite-wal",
        workspace / "faultflow.sqlite-shm",
        workspace / "faultflow.sqlite-journal",
    ]
    for path in db_files:
        path.write_text("db", encoding="utf-8")

    assert runner._clean_workspace() >= 1

    assert keep.exists()
    assert not (workspace / "faultflow.sqlite").exists()


def test_missing_nl2bench_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "cell_lib = cells/sky130/sky130_fd_sc_hd.json",
            f"cell_lib = {tmp_path / 'cells.json'}\nliberty = {tmp_path / 'cells.lib'}",
        ),
        encoding="utf-8",
    )
    (tmp_path / "cells.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cells.lib").write_text("", encoding="utf-8")
    out = tmp_path / "output" / "demo"
    out.mkdir(parents=True)
    intermediate = out / ".faultflow" / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "demo_gate.v").write_text(
        "module demo; endmodule\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _name: None)

    with pytest.raises(RunnerError, match="nl2bench"):
        Runner(load_config(cfg, "demo"))._run_nl2bench()


def test_quaigh_receives_bench_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    out = tmp_path / "output" / "demo"
    out.mkdir(parents=True)
    intermediate = out / ".faultflow" / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "demo.bench").write_text(
        "INPUT(a)\nOUTPUT(y)\ny = BUFF(a)\n", encoding="utf-8"
    )
    seen: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    vector_path = Runner(load_config(cfg, "demo"))._run_quaigh()

    assert vector_path == Path("output/demo/patterns.test")
    assert seen["cmd"][2].endswith(".bench")
    assert all(not arg.endswith(".blif") for arg in seen["cmd"])


def test_verilog_netlist_runs_yosys_instead_of_simulating_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    verilog = tmp_path / "demo.v"
    verilog.write_text("module demo(input a, output y); assign y = a; endmodule\n")
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "netlist = missing.json", f"netlist = {verilog}"
        ),
        encoding="utf-8",
    )
    runner = Runner(load_config(cfg, "demo"))
    generated = Path("output/demo/.faultflow/intermediate/demo.json")
    monkeypatch.setattr(runner, "_run_yosys", lambda: generated)

    assert runner._find_netlist() == generated


def test_init_without_json_defers_fingerprint_until_sim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    verilog = tmp_path / "demo.v"
    verilog.write_text("module demo(input a, output y); assign y = a; endmodule\n")
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "netlist = missing.json", f"netlist = {verilog}"
        ),
        encoding="utf-8",
    )

    assert main(["init", "--top", "demo", "-c", str(cfg)]) == 0
    with connect(tmp_path / "output/demo/.faultflow/faultflow.sqlite") as conn:
        init_schema(conn)
        row = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()

    assert row is not None
    assert row[0] == 0


def test_yosys_version_extraction_ignores_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    runner = Runner(load_config(cfg, "demo"))
    runner.cfg.ensure_workspace()
    (runner.cfg.logs_dir / "yosys.log").write_text(
        """
 /----------------------------------------------------------------------------\\
 |  yosys -- Yosys Open SYnthesis Suite                                       |
 \\----------------------------------------------------------------------------/
 Yosys 0.61+129 (git sha1 6dbe03f0f, g++ 13.3.0 -fPIC -O3)
""",
        encoding="utf-8",
    )

    assert runner._extract_yosys_version().startswith("0.61+129")


def test_coverage_report_schema_and_denominator_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[3] / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    db_path = cfg.db_path
    conn = connect(db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn)
        insert_run(
            conn,
            campaign_id,
            vector_count=3,
            atpg_terminal_reason="THRESHOLD_MET",
            atpg_rounds=2,
            atpg_sat=4,
            atpg_unsat=1,
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="detected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa1",
            status="undetected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=2,
            net_name="b",
            compiled_net_index=2,
            fault_type="sa0",
            status="undetected",
            collapsed_into=1,
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=3,
            net_name="clk",
            compiled_net_index=3,
            fault_type="sa0",
            status="excluded",
            exclusion="clock",
        )
        conn.commit()

        json_path, txt_path, report = write_reports(conn, cfg, campaign_id=campaign_id)

        assert json_path.exists()
        assert txt_path.exists()
        assert report["policy"]["unsupported_cells"] == "fail"
        assert report["summary"]["denominator"] == 2
        assert report["undetected_faults"][0]["fault_site_key"] == "net:1:stem"
        assert report["undetected_faults"][0]["protocol_unresolved"] is False
        assert report["run"]["atpg_terminal_reason"] == "THRESHOLD_MET"
        assert report["run"]["atpg_rounds"] == 2
        assert report["run"]["atpg_sat"] == 4
    finally:
        conn.close()


def _parse_coverage_rpt(text: str) -> dict[str, float]:
    """Pull the numeric summary fields out of coverage.rpt's ``key: value`` lines.

    Field names in the .rpt use a shorthand (``fault_coverage_%`` instead of
    ``fault_coverage_percent``) — map them to their coverage_report.json keys.
    """
    rename = {
        "fault_coverage_%": "fault_coverage_percent",
        "test_coverage_%": "test_coverage_percent",
        "coverage_percent": "coverage_percent",
    }
    values: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if key not in (
            "total_raw_faults",
            "structural_eligible",
            "denominator",
            "detected",
            "undetected",
            "redundant",
            "collapsed",
            "excluded_blackbox",
            "excluded_clock",
            "excluded_reset",
            "fault_coverage_%",
            "test_coverage_%",
            "coverage_percent",
        ):
            continue
        values[rename.get(key, key)] = float(raw.strip())
    return values


def test_coverage_rpt_and_json_agree_on_summary_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """coverage.rpt (text) and coverage_report.json are both written by
    write_reports() from the SAME in-memory `data`/`report` dict (see
    faultflow/reporter/coverage.py::write_reports) — never computed
    independently. Parse the .rpt's numeric summary fields and assert they
    match coverage_report.json's `summary` numerically, for a real generated
    report, so any future refactor that lets them drift apart is caught."""
    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[3] / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn)
        insert_run(conn, campaign_id, vector_count=5)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="detected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa1",
            status="undetected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=2,
            net_name="b",
            compiled_net_index=2,
            fault_type="sa0",
            status="undetected",
            collapsed_into=1,
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=3,
            net_name="clk",
            compiled_net_index=3,
            fault_type="sa0",
            status="excluded",
            exclusion="clock",
        )
        conn.commit()

        json_path, txt_path, report = write_reports(conn, cfg, campaign_id=campaign_id)

        json_summary = json.loads(json_path.read_text(encoding="utf-8"))["summary"]
        rpt_values = _parse_coverage_rpt(txt_path.read_text(encoding="utf-8"))

        assert rpt_values, "expected numeric summary fields in coverage.rpt"
        for key, rpt_value in rpt_values.items():
            assert key in json_summary, f"{key} in .rpt but not in .json summary"
            assert rpt_value == pytest.approx(
                json_summary[key]
            ), f"{key} mismatch: rpt={rpt_value} json={json_summary[key]}"
    finally:
        conn.close()


def test_coverage_report_text_includes_protocol_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[3] / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "scan_top")
    db_path = cfg.db_path
    conn = connect(db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        insert_run(conn, campaign_id)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=9,
            net_name="__ppo_u0",
            compiled_net_index=9,
            fault_type="sa0",
            status="undetected",
            fault_site_key="net:9:stem",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        conn.execute(
            "UPDATE faults SET protocol_unresolved = 1 WHERE id = ?",
            (fault_id,),
        )
        conn.commit()

        _, txt_path, report = write_reports(conn, cfg, campaign_id=campaign_id)
        text = txt_path.read_text(encoding="utf-8")
        assert report["summary"]["protocol_unresolved"] == 1
        assert "protocol_unresolved: 1" in text
        assert "fault_coverage_%:" in text
        assert "test_coverage_%:" in text
        # The per-fault undetected line now surfaces protocol-unresolved as its
        # reason; the structured report carries the reason + a gap breakdown.
        undetected_section = text.split("undetected faults:")[-1]
        assert "reason=structurally_unresolved" in undetected_section
        assert report["undetected_faults"][0]["reason"] == "structurally_unresolved"
        assert report["reason_summary"]["structurally_unresolved"] == 1
    finally:
        conn.close()


def test_coverage_report_classifies_sat_timeout_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[3] / "schemas/coverage.schema.json",
        schema_dir / "coverage.schema.json",
    )
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "scan_top")
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="scan", top="scan_top")
        insert_run(conn, campaign_id)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=9,
            net_name="__ppo_u0",
            compiled_net_index=9,
            fault_type="sa0",
            status="undetected",
            fault_site_key="net:9:stem",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        # What the ATPG loop does at round end for a fault the solver timed out on.
        record_sat_outcomes(conn, campaign_id, {fault_id: "timeout"}, round_idx=1)
        conn.commit()

        _, txt_path, report = write_reports(conn, cfg, campaign_id=campaign_id)
        text = txt_path.read_text(encoding="utf-8")
        fault = report["undetected_faults"][0]
        assert fault["reason"] == "sat_timeout"
        assert fault["sat_outcome"] == "timeout"
        assert report["reason_summary"]["sat_timeout"] == 1
        assert "reason=sat_timeout" in text.split("undetected faults:")[-1]
    finally:
        conn.close()


def test_coverage_report_names_reconvergent_bottleneck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a SAT-hard (timed-out) undetected fault whose cone contains a
    persisted reconvergent stem gets that stem named as its bottleneck_net."""
    monkeypatch.chdir(tmp_path)
    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension required")
    root = Path(__file__).resolve().parents[3]
    c17 = root / "tests/benchmarks/iscas85/synth_sky130/c17.json"
    sky = root / "cells/sky130/sky130_fd_sc_hd.json"
    if not c17.exists():
        pytest.skip("c17 netlist missing")
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir(parents=True)
    shutil.copy(
        root / "schemas/coverage.schema.json", schema_dir / "coverage.schema.json"
    )

    site = next(
        s
        for s in core.list_site_keys(str(c17), str(sky), "fail")
        if int(s["yosys_net_id"]) >= 0
    )
    net_id = int(site["yosys_net_id"])
    cidx = int(site["compiled_net_index"])

    cfg_path = tmp_path / "c17.ofs"
    cfg_path.write_text(
        f"[design]\nnetlist = {c17}\ncell_lib = {sky}\n"
        "[simulation]\nunsupported_cells = fail\n"
        "[atpg]\nmode = comb\n[report]\nthreshold = 95.0\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, "c17")
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn, campaign_type="comb", top="c17")
        insert_run(conn, campaign_id)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=net_id,
            net_name="n_bott",
            compiled_net_index=cidx,
            fault_type="sa0",
            status="undetected",
            fault_site_key=f"net:{net_id}:stem",
        )
        fault_id = int(conn.execute("SELECT id FROM faults").fetchone()[0])
        record_sat_outcomes(conn, campaign_id, {fault_id: "timeout"}, round_idx=1)
        # The fault's own net is a reconvergent stem -> it is its own bottleneck.
        record_reconvergent_stems(conn, campaign_id, {net_id})
        conn.commit()

        _, _txt, report = write_reports(conn, cfg, campaign_id=campaign_id)
        fault = report["undetected_faults"][0]
        assert fault["reason"] == "sat_timeout"
        assert fault["bottleneck_net"] == "n_bott"
    finally:
        conn.close()


def test_ext_without_bench_sidecar_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    netlist = tmp_path / "demo.json"
    netlist.write_text(
        '{"creator":"yosys","modules":{"demo":{"attributes":{"top":1},'
        '"ports":{"a":{"direction":"input","bits":[2]},'
        '"y":{"direction":"output","bits":[3]}},"cells":{}}}}',
        encoding="utf-8",
    )
    cfg.write_text(
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

    runner = Runner(load_config(cfg, "demo"))
    runner.cfg.ensure_workspace()
    runner.cfg.db_path.write_bytes(b"")

    with pytest.raises(RunnerError, match="BENCH sidecar"):
        runner.sim(ext=ext)


def test_coverage_report_rejects_denominator_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    db_path = cfg.db_path
    conn = connect(db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="a",
            compiled_net_index=1,
            fault_type="sa0",
            status="detected",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=2,
            net_name="bb",
            compiled_net_index=2,
            fault_type="sa0",
            status="excluded",
            exclusion="blackbox",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=3,
            net_name="mystery",
            compiled_net_index=3,
            fault_type="sa0",
            status="excluded",
            exclusion="other",
        )
        conn.commit()

        with pytest.raises(CoverageError, match="denominator invariant"):
            write_reports(conn, cfg, campaign_id=campaign_id)
    finally:
        conn.close()


def test_coverage_report_rejects_zero_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Policy 3: a campaign whose entire fault population is excluded (or
    # collapsed) has a zero denominator and MUST raise rather than emit a
    # NaN / None coverage number.
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        campaign_id = insert_campaign(conn)
        insert_fault_row(
            conn,
            campaign_id,
            net_id=1,
            net_name="clk",
            compiled_net_index=1,
            fault_type="sa0",
            status="excluded",
            exclusion="clock",
        )
        insert_fault_row(
            conn,
            campaign_id,
            net_id=2,
            net_name="bb",
            compiled_net_index=2,
            fault_type="sa0",
            status="excluded",
            exclusion="blackbox",
        )
        conn.commit()

        with pytest.raises(CoverageError, match="denominator is zero"):
            write_reports(conn, cfg, campaign_id=campaign_id)
    finally:
        conn.close()
