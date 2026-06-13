from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import faultflow.cli as cli_mod
import faultflow.runner.runner as runner_mod
from faultflow.atpg import (
    PatternError,
    VectorSet,
    parse_bench_io,
    parse_bench_outputs,
)
from faultflow.cli import main
from faultflow.config import ConfigError, load_config
from faultflow.runner import Runner, RunnerError
from faultflow.verify import (
    CycleSpec,
    IverilogVerifier,
    SequentialStep,
    VerificationError,
    VectorContract,
    parse_iverilog_samples,
    render_testbench,
)


def _config(path: Path, extra_sim: str = "") -> None:
    path.write_text(
        f"""
[design]
netlist = missing.json
cell_lib = cells/osu/osu035.json
liberty = cells/osu/osu035_stdcells.lib

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
{extra_sim}

[atpg]
mode = comb
output = missing.test

[report]
threshold = 95.0
""".strip() + "\n",
        encoding="utf-8",
    )


def test_config_verify_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)

    cfg = load_config(cfg_path, "demo")

    assert cfg.simulation.verify is False
    assert cfg.simulation.verify_tool == "iverilog"
    assert cfg.verilog_models == Path("cells/sky130/sky130_fd_sc_hd.v")


def test_cli_verify_override_reaches_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)
    seen: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, cfg: object) -> None:
            seen["cfg"] = cfg

        def sim(
            self,
            purge: bool = False,
            clean: bool = False,
            verify: bool | None = None,
            ext: Path | None = None,
            max_rounds: int | None = None,
            target_coverage: float | None = None,
            scan: bool = False,
        ) -> str:
            seen["purge"] = purge
            seen["clean"] = clean
            seen["verify"] = verify
            seen["ext"] = ext
            seen["max_rounds"] = max_rounds
            seen["target_coverage"] = target_coverage
            seen["scan"] = scan
            return "ok"

    monkeypatch.setattr(cli_mod, "Runner", FakeRunner)

    assert main(["sim", "--top", "demo", "-c", str(cfg_path), "-v", "true"]) == 0

    assert seen["purge"] is False
    assert seen["verify"] is True


def test_cli_invalid_verify_bool_fails(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)

    with pytest.raises(SystemExit) as exc:
        main(["sim", "--top", "demo", "-c", str(cfg_path), "-v", "maybe"])

    assert exc.value.code == 2


def test_unsupported_verify_tool_fails(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path, "verify_tool = verilator")

    with pytest.raises(ConfigError, match="verify_tool"):
        load_config(cfg_path, "demo")


def test_systemverilog_input_fails_until_sv_is_supported(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    rtl = tmp_path / "demo.sv"
    rtl.write_text(
        "module demo(input logic a, output logic y); assign y = a; endmodule\n"
    )
    _config(cfg_path)
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "netlist = missing.json", f"netlist = {rtl}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunnerError, match="SystemVerilog"):
        Runner(load_config(cfg_path, "demo"))._find_verilog_source()


def test_parse_bench_inputs_and_outputs(tmp_path: Path) -> None:
    bench = tmp_path / "demo.bench"
    bench.write_text(
        "# comment\nINPUT(a)\nPINPUT(b)\nOUTPUT(y)\nOUTPUT(z)\n",
        encoding="utf-8",
    )

    ports = parse_bench_io(bench)

    assert ports.inputs == ["a", "b"]
    assert ports.outputs == ["y", "z"]
    assert parse_bench_outputs(bench) == ["y", "z"]


def test_parse_bench_outputs_required(tmp_path: Path) -> None:
    bench = tmp_path / "bad.bench"
    bench.write_text("INPUT(a)\n", encoding="utf-8")

    with pytest.raises(PatternError, match="outputs"):
        parse_bench_outputs(bench)


def test_vector_contract_combinational_defaults() -> None:
    contract = VectorContract.combinational()
    cycle = CycleSpec.combinational()

    assert contract.sample_on_cycle == 0
    assert contract.is_sample_cycle(cycle)
    assert not contract.is_settle_cycle(cycle)
    assert cycle.clock_edge == "NONE"
    assert cycle.sample_outputs is True


def test_rendered_testbench_drives_pis_and_samples_pos() -> None:
    vectors = VectorSet(
        source="demo.test",
        input_order=["a", "b"],
        vectors=[{"a": False, "b": True}],
    )

    text = render_testbench("demo", ["a", "b"], ["y", "z"], vectors)

    assert "reg a;" in text
    assert "reg b;" in text
    assert "wire y;" in text
    assert "wire z;" in text
    assert "a = 1'b0;" in text
    assert "b = 1'b1;" in text
    assert '$display("FFVERIFY_VECTOR %0d %b", 1, {y, z});' in text


def test_rendered_testbench_supports_sequential_steps() -> None:
    vectors = VectorSet(
        source="tiny_dff.seq",
        input_order=["clk", "d"],
        vectors=[{"clk": False, "d": False}],
    )
    steps = [
        [
            SequentialStep(
                inputs={"clk": False, "d": True},
                cycle=CycleSpec(
                    cycle=0,
                    clock_edge="NONE",
                    reset_state=0,
                    sample_outputs=False,
                ),
            ),
            SequentialStep(
                inputs={"clk": True, "d": True},
                cycle=CycleSpec(
                    cycle=1,
                    clock_edge="POSEDGE",
                    reset_state=0,
                    sample_outputs=True,
                ),
            ),
        ]
    ]

    text = render_testbench("tiny_dff", ["clk", "d"], ["q"], vectors, steps)

    assert "clk = 1'b0;" in text
    assert "clk = 1'b1;" in text
    assert text.count("FFVERIFY_VECTOR") == 1
    assert '$display("FFVERIFY_VECTOR %0d %b", 1, {q});' in text


def test_rendered_testbench_rejects_missing_pi() -> None:
    vectors = VectorSet(source="demo.test", input_order=["a"], vectors=[{}])

    with pytest.raises(VerificationError, match="missing PI a"):
        render_testbench("demo", ["a"], ["y"], vectors)


def test_parse_iverilog_samples_rejects_x_and_z() -> None:
    with pytest.raises(VerificationError, match="output y"):
        parse_iverilog_samples("FFVERIFY_VECTOR 1 x\n", ["y"])

    with pytest.raises(VerificationError, match="output z"):
        parse_iverilog_samples("FFVERIFY_VECTOR 1 z\n", ["z"])


def test_parse_iverilog_samples_returns_outputs() -> None:
    parsed = parse_iverilog_samples("noise\nFFVERIFY_VECTOR 1 10\n", ["y", "z"])

    assert parsed == [{"y": True, "z": False}]


def test_iverilog_missing_tool_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _name: None)
    verifier = IverilogVerifier(
        top="demo",
        work_dir=tmp_path,
        gate_verilog=tmp_path / "demo.v",
        verilog_models=[tmp_path / "models.v"],
    )

    with pytest.raises(VerificationError, match="iverilog"):
        verifier.run(["a"], ["y"], VectorSet("demo.test", ["a"], [{"a": True}]))


def test_iverilog_verifier_rejects_x_and_z_outputs(tmp_path: Path) -> None:
    model = tmp_path / "models.v"
    model.write_text("", encoding="utf-8")
    vectors = VectorSet("demo.test", ["a"], [{"a": True}])

    for value, expected in [("1'bx", "output y"), ("1'bz", "output y")]:
        gate = tmp_path / f"demo_{value[-1]}.v"
        gate.write_text(
            f"module demo(input a, output y); assign y = {value}; endmodule\n",
            encoding="utf-8",
        )
        verifier = IverilogVerifier(
            top="demo",
            work_dir=tmp_path / f"verify_{value[-1]}",
            gate_verilog=gate,
            verilog_models=[model],
        )

        with pytest.raises(VerificationError, match=expected):
            verifier.run(["a"], ["y"], vectors)


def test_iverilog_verifier_collects_binary_outputs(tmp_path: Path) -> None:
    model = tmp_path / "models.v"
    model.write_text("", encoding="utf-8")
    gate = tmp_path / "demo.v"
    gate.write_text(
        "module demo(input a, output y); assign y = a; endmodule\n",
        encoding="utf-8",
    )
    vectors = VectorSet("demo.test", ["a"], [{"a": True}, {"a": False}])
    verifier = IverilogVerifier(
        top="demo",
        work_dir=tmp_path / "verify",
        gate_verilog=gate,
        verilog_models=[model],
    )

    result = verifier.run(["a"], ["y"], vectors)

    assert result.passed is True
    assert result.expected_outputs == [{"y": True}, {"y": False}]
    assert (tmp_path / "verify" / "testbench.v").exists()


def test_cpp_sequence_helper_matches_iverilog_tiny_dff(tmp_path: Path) -> None:
    core = runner_mod._load_core()
    assert core is not None

    gate = tmp_path / "tiny_dff.v"
    gate.write_text(
        """
module tiny_dff(input CLK, input D, output Q);
  DFFPOSX1 u0(.CLK(CLK), .D(D), .Q(Q));
endmodule
""".strip() + "\n",
        encoding="utf-8",
    )
    vectors = VectorSet(
        source="tiny_dff.seq",
        input_order=["CLK", "D"],
        vectors=[{"CLK": False, "D": False}],
    )
    steps = [
        [
            SequentialStep(
                inputs={"CLK": False, "D": True},
                cycle=CycleSpec(0, "NONE", 0, False),
            ),
            SequentialStep(
                inputs={"CLK": True, "D": True},
                cycle=CycleSpec(1, "POSEDGE", 0, True),
            ),
        ]
    ]
    verifier = IverilogVerifier(
        top="tiny_dff",
        work_dir=tmp_path / "verify",
        gate_verilog=gate,
        verilog_models=[Path("cells/osu/osu035_stdcells.v")],
    )

    iverilog_result = verifier.run(["CLK", "D"], ["Q"], vectors, steps)
    cpp_result = core.fault_free_sequence_outputs(
        "tests/cpp/fixtures/tiny_dff.json",
        "cells/osu/osu035.json",
        [[{"CLK": False, "D": True}, {"CLK": True, "D": True}]],
        ["CLK", "D"],
        ["Q"],
        "fail",
    )

    assert cpp_result == iverilog_result.expected_outputs == [{"Q": True}]


def test_runner_verification_failure_aborts_before_sim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path, "verify = true")
    cfg = load_config(cfg_path, "demo")
    runner = Runner(cfg)
    vector_path = tmp_path / "demo.test"
    vector_path.write_text("1: 1\n", encoding="utf-8")
    bench = tmp_path / "demo.bench"
    bench.write_text("INPUT(a)\nOUTPUT(y)\n", encoding="utf-8")
    called = {"report": False}
    vectors = VectorSet(
        source=str(vector_path),
        input_order=["a"],
        vectors=[{"a": True}],
    )

    monkeypatch.setattr(runner, "_find_netlist", lambda: tmp_path / "demo.json")
    monkeypatch.setattr(
        Runner, "_ensure_campaign", lambda self, conn, fp, scan=False: 1
    )
    monkeypatch.setattr(runner, "_find_order_sidecar", lambda: (bench, ["a"]))
    monkeypatch.setattr(
        runner,
        "_run_verification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RunnerError("verification failed")
        ),
        raising=False,
    )

    def fake_progressive(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        from faultflow.runner.progressive_atpg import AtpgStats

        return vectors, AtpgStats(terminal_reason="COMPLETE"), 1, 0.0, 0.0

    monkeypatch.setattr(
        "faultflow.runner.progressive_atpg.run_progressive_native_atpg",
        fake_progressive,
    )

    def fake_write_reports(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        called["report"] = True
        report_path = tmp_path / "output" / "demo" / ".faultflow" / "intermediate" / "coverage_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return (
            report_path,
            tmp_path / "output" / "demo" / "coverage.rpt",
            {"summary": {"coverage_percent": 100.0}},
        )

    monkeypatch.setattr("faultflow.runner.runner.write_reports", fake_write_reports)

    with pytest.raises(RunnerError, match="verification failed"):
        runner.sim()

    assert called["report"] is False
    assert not (tmp_path / "output" / "demo" / ".faultflow" / "intermediate" / "coverage_report.json").exists()


def test_runner_verification_dependency_failure_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path, f"verify = true\nverilog_models = {tmp_path / 'missing.v'}")
    cfg = load_config(cfg_path, "demo")
    runner = Runner(cfg)
    bench = tmp_path / "demo.bench"
    bench.write_text("INPUT(a)\nOUTPUT(y)\n", encoding="utf-8")
    gate = tmp_path / "demo_gate.v"
    gate.write_text(
        "module demo(input a, output y); assign y = a; endmodule\n",
        encoding="utf-8",
    )
    vectors = VectorSet("demo.test", ["a"], [{"a": True}])

    monkeypatch.setattr(runner, "_find_gate_verilog", lambda: gate)

    with pytest.raises(RunnerError, match="missing verilog_models"):
        runner._run_verification(tmp_path / "demo.json", bench, vectors, ["a"])

    report = (
        tmp_path
        / "output"
        / "demo"
        / ".faultflow"
        / "verification"
        / "verification_report.json"
    )
    assert report.exists()
    assert '"passed": false' in report.read_text(encoding="utf-8")


def test_runner_updates_verified_vectors_after_sim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path, "verify = true")
    cfg = load_config(cfg_path, "demo")
    runner = Runner(cfg)
    cfg.ensure_workspace()
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("""
        CREATE TABLE vectors (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL,
          source TEXT NOT NULL,
          vector_index INTEGER NOT NULL,
          pattern TEXT NOT NULL,
          inputs TEXT NOT NULL DEFAULT '{}',
          expected TEXT NOT NULL DEFAULT '{}',
          verified INTEGER NOT NULL DEFAULT 0
        )
        """)
    conn.execute("""
        INSERT INTO vectors(run_id, source, vector_index, pattern)
        VALUES (7, 'demo.test', 1, '1')
        """)
    conn.commit()
    conn.close()

    vectors = VectorSet("demo.test", ["a"], [{"a": True}])
    expected = [{"y": False}]

    runner._write_verified_vectors(7, vectors, expected)

    conn = sqlite3.connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT inputs, expected, verified FROM vectors WHERE run_id = 7"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == '{"a": true}'
    assert row[1] == '{"y": false}'
    assert row[2] == 1


def test_cpp_fault_free_output_helper_matches_tiny_buf() -> None:
    core = runner_mod._load_core()
    assert core is not None

    outputs = core.fault_free_outputs(
        "tests/cpp/fixtures/tiny_buf.json",
        "cells/osu/osu035.json",
        [{"A": True}, {"A": False}],
        ["A"],
        ["Y"],
        "fail",
    )

    assert outputs == [{"Y": True}, {"Y": False}]
