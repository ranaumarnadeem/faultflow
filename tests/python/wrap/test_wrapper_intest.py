"""INTEST of the IEEE 1500 wrapper ff.py wrap puts on: a scan test of the wrapped
block with its input cells holding and its output cells safe, the mode pins held at
(1, 0), and whatever drives its ports unknown (faultflow.wrap.environment).

Each flow's exported patterns replay on the block's sky130 cells with the wrapped
inputs at X, both overlap modes, and as STIL (scan_replay); every fault they credit
is detected in the full scan protocol, by one pattern whatever the environment
drives (scan_credit). The block: clk 2, d0 4, d1 5; r0 = d0 ^ r2, r1 = r0 & d1,
r2 = r1 | d0; y = r2 ^ r0."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scan_credit import credit_not_reproduced, environment_dependent
from scan_replay import replay_on_cells
from wrap_flow import faults as fault_rows
from wrap_flow import ACCUMULATOR, run_mode

ENVIRONMENT = ["d0", "d1"]


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


@pytest.mark.integration
def test_intest_patterns_test_the_core_whatever_drives_the_block(
    tmp_path: Path, flow_tools: None
) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(tmp_path, "intest")
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported
        for pattern in exported:
            values = pattern["capture_pi_values"]
            assert (values["wbr_intest"], values["wbr_extest"]) == (True, False)
            # Nothing of the environment: no wrapped input set, no output compared.
            assert not {"d0", "d1", "y"} & set(values)
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=ENVIRONMENT
        )
        assert problems == []
        assert credit_not_reproduced(cfg, patterns) == []
        assert environment_dependent(cfg, patterns, ENVIRONMENT) == []
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        report = json.loads(
            (cfg.intermediate_dir / "coverage_report.json").read_text("utf-8")
        )
        text = cfg.coverage_report_path.read_text(encoding="utf-8")
        faults = {(row["fault_site_key"], row["type"]): row for row in fault_rows(cfg)}
    wrapper = manifest["wrapper"]
    intest_net = wrapper["intest"]["net"]
    # INTEST stuck at 0 would let the environment into the core: no test of it.
    assert faults[(f"net:{intest_net}:stem", "sa0")]["blackbox_unresolved"] == 1
    # The system side is EXTEST's.
    for cell in wrapper["cells"]:
        for fault_type in ("sa0", "sa1"):
            row = faults[(f"net:{cell['sys_net']}:stem", fault_type)]
            assert row["exclusion"] == "wbr_decoupled"
    assert faults[(f"net:{intest_net}:stem", "sa1")]["exclusion"] == "wbr_decoupled"
    summary = report["summary"]
    assert summary["detected"] > 0
    # The report by part adds up to its summary.
    assert (report["wrapper"]["mode"], report["wrapper"]["cells"]) == ("intest", 3)
    parts = report["wrapper"]["parts"]
    for key, total in (
        ("detected", "detected"),
        ("denominator", "denominator"),
        ("decoupled", "excluded_wbr_decoupled"),
        ("blackbox_unresolved", "blackbox_unresolved"),
    ):
        assert sum(part[key] for part in parts.values()) == summary[total], key
    assert parts["boundary"]["decoupled"] > 0
    assert parts["mode"]["blackbox_unresolved"] >= 1
    assert parts["core"]["detected"] > 0
    assert "wrapper (intest, 3 boundary cells)" in text


@pytest.mark.integration
@pytest.mark.parametrize("launch", ["loc", "los"])
def test_intest_transition_patterns_test_the_core_whatever_drives_the_block(
    tmp_path: Path, flow_tools: None, launch: str
) -> None:
    """Launch on capture: the input cells hold through launch and capture, so the
    core sees their flops' values in both and no transition starts at an input
    cell: those are testable only if the environment drove the core
    (blackbox_unresolved). Launch on shift: the last shift launches them too.
    Either way a held mode net never transitions -- no transition fault lets the
    environment in, so none is marked before the test -- and a transition on one
    is testable only with the mode pins free."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(
            tmp_path,
            "intest",
            f"[fault_model]\nmodel = transition\nlaunch = {launch}\n"
            "collapsing = false\n",
        )
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported and {p["launch"] for p in exported} == {launch}
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=ENVIRONMENT
        )
        assert problems == []
        if launch == "loc":
            assert credit_not_reproduced(cfg, patterns, loc=True) == []
            assert environment_dependent(cfg, patterns, ENVIRONMENT, loc=True) == []
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        faults = {(row["fault_site_key"], row["type"]): row for row in fault_rows(cfg)}
    wrapper = manifest["wrapper"]
    mode_nets = {wrapper["intest"]["net"], wrapper["extest"]["net"]}
    unresolved = {key for key, row in faults.items() if row["blackbox_unresolved"]}
    on_mode_nets = {key for key in unresolved if int(key[0].split(":")[1]) in mode_nets}
    assert on_mode_nets
    for cell in wrapper["cells"]:
        if cell["side"] != "input":
            continue
        for fault_type in ("sa0", "sa1"):
            core_side = faults[(f"net:{cell['core_net']}:stem", fault_type)]
            if launch == "loc":
                assert core_side["blackbox_unresolved"] == 1
            else:
                assert core_side["status"] == "detected"
    if launch == "los":
        assert unresolved == on_mode_nets


@pytest.mark.integration
@pytest.mark.parametrize("model", ["stuck-at", "loc", "los"])
def test_a_synthesized_blocks_intest_patterns_hold_whatever_drives_it(
    tmp_path: Path, flow_tools: None, model: str
) -> None:
    """A block Yosys synthesizes from RTL: an accumulator with an asynchronous
    reset, which stays unwrapped and held inactive, and b[1] left unwrapped
    ([wrap] exclude): the tester drives it like any input, and the harness keeps it
    as a one-bit port of its own."""
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    fault_model = (
        ""
        if model == "stuck-at"
        else "[fault_model]\nmodel = transition\n"
        f"launch = {model}\ncollapsing = false\n"
    )
    environment = ["en", "a[0]", "a[1]", "b[0]"]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(
            tmp_path,
            "intest",
            fault_model,
            netlist=rtl,
            top="acc",
            wrap="exclude = b[1]\n",
        )
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        assert [c["label"] for c in manifest["wrapper"]["cells"]] == [
            *environment,
            "sum[0]",
            "sum[1]",
            "carry",
        ]
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported
        assert all("b[1]" in pattern["capture_pi_values"] for pattern in exported)
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=environment
        )
        assert problems == []
        if model != "los":
            loc = model == "loc"
            assert credit_not_reproduced(cfg, patterns, loc=loc) == []
            assert environment_dependent(cfg, patterns, environment, loc=loc) == []
