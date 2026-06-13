from pathlib import Path
from types import SimpleNamespace

import pytest

from faultflow.atpg import VectorSet
from faultflow.cli import main
from faultflow.config import load_config
from faultflow.db import connect, init_schema
from db_v3_helpers import insert_campaign, insert_fault_row, insert_run
from faultflow.reporter import CoverageError, write_reports
from faultflow.runner import Runner, RunnerError
import faultflow.runner.runner as runner_mod


def _config(path: Path) -> None:
    path.write_text(
        """
[design]
netlist = missing.json
cell_lib = cells/osu/osu035.json

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
output = missing.test

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
    assert (tmp_path / "output" / "demo" / "faultflow.sqlite").exists()


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


def test_clean_db_removes_only_sqlite_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    runner = Runner(load_config(cfg_path, "demo"))
    out = tmp_path / "output" / "demo"
    out.mkdir(parents=True)
    keep = out / "coverage_report.json"
    keep.write_text("{}", encoding="utf-8")
    db_files = [
        out / "faultflow.sqlite",
        out / "faultflow.sqlite-wal",
        out / "faultflow.sqlite-shm",
        out / "faultflow.sqlite-journal",
    ]
    for path in db_files:
        path.write_text("db", encoding="utf-8")

    assert runner._clean_db() == 4

    assert keep.exists()
    assert all(not path.exists() for path in db_files)


def test_missing_nl2bench_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "cell_lib = cells/osu/osu035.json",
            f"cell_lib = {tmp_path / 'cells.json'}\nliberty = {tmp_path / 'cells.lib'}",
        ),
        encoding="utf-8",
    )
    (tmp_path / "cells.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cells.lib").write_text("", encoding="utf-8")
    out = tmp_path / "output" / "demo"
    out.mkdir(parents=True)
    (out / "demo_gate.v").write_text("module demo; endmodule\n", encoding="utf-8")
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
    (out / "demo.bench").write_text(
        "INPUT(a)\nOUTPUT(y)\ny = BUFF(a)\n", encoding="utf-8"
    )
    seen: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    vector_path = Runner(load_config(cfg, "demo"))._run_quaigh()

    assert vector_path == Path("output/demo/demoatpg.test")
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
    generated = Path("output/demo/demo.json")
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
    with connect(tmp_path / "output/demo/faultflow.sqlite") as conn:
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
    runner.cfg.output_dir.mkdir(parents=True)
    (runner.cfg.output_dir / "yosys.log").write_text(
        """
 /----------------------------------------------------------------------------\\
 |  yosys -- Yosys Open SYnthesis Suite                                       |
 \\----------------------------------------------------------------------------/
 Yosys 0.61+129 (git sha1 6dbe03f0f, g++ 13.3.0 -fPIC -O3)
""",
        encoding="utf-8",
    )

    assert runner._extract_yosys_version().startswith("0.61+129")


def test_coverage_report_schema_and_denominator_invariant(tmp_path: Path) -> None:
    db_path = tmp_path / "faultflow.sqlite"
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

        json_path, txt_path, report = write_reports(
            conn, tmp_path, "demo", campaign_id=campaign_id
        )

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


def test_coverage_report_text_includes_protocol_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "faultflow.sqlite"
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

        _, txt_path, report = write_reports(
            conn, tmp_path, "scan_top", campaign_id=campaign_id
        )
        text = txt_path.read_text(encoding="utf-8")
        assert report["summary"]["protocol_unresolved"] == 1
        assert "protocol_unresolved: 1" in text
        assert "fault_coverage_%:" in text
        assert "test_coverage_%:" in text
        assert "protocol_unresolved" in text.split("undetected faults:")[-1]
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
cell_lib = {Path(__file__).resolve().parents[2] / "cells/osu/osu035.json"}

[simulation]
unsupported_cells = fail
""".strip() + "\n",
        encoding="utf-8",
    )
    ext = tmp_path / "vectors.test"
    ext.write_text("vector\na=0\n", encoding="utf-8")

    runner = Runner(load_config(cfg, "demo"))
    runner.cfg.output_dir.mkdir(parents=True, exist_ok=True)
    (runner.cfg.output_dir / "faultflow.sqlite").write_bytes(b"")

    with pytest.raises(RunnerError, match="BENCH sidecar"):
        runner.sim(ext=ext)


def test_coverage_report_rejects_denominator_invariant(tmp_path: Path) -> None:
    db_path = tmp_path / "faultflow.sqlite"
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
            write_reports(conn, tmp_path, "demo", campaign_id=campaign_id)
    finally:
        conn.close()
