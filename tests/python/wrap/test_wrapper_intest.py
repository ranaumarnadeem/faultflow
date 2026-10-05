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
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from scan_credit import credit_not_reproduced, environment_dependent
from scan_replay import replay_on_cells

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "chip"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
ENVIRONMENT = ["d0", "d1"]


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    outputs = ("Q", "X")
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _gate(kind: str, a: int, b: int, x: int) -> dict[str, Any]:
    return _cell(f"sky130_fd_sc_hd__{kind}_1", A=a, B=b, X=x)


def _ring() -> dict[str, Any]:
    cells = {
        "g_x": _gate("xor2", 4, 19, 14),
        "r0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_a": _gate("and2", 15, 5, 16),
        "r1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 17, 4, 18),
        "r2": _cell(DFXTP, CLK=2, D=18, Q=19),
        "g_y": _gate("xor2", 19, 15, 20),
    }
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in (("clk", 2), ("d0", 4))},
        "d1": {"direction": "input", "bits": [5]},
        "y": {"direction": "output", "bits": [20]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _intest(
    work: Path,
    sections: str = "",
    netlist: Path | None = None,
    top: str = TOP,
    wrap: str = "",
) -> tuple[Any, Path]:
    """init, scan (the wrapper first), scan-check and intest with exported patterns
    on `netlist` (the ring by default; RTL Yosys synthesizes), run in `work`: the
    config and the patterns' path. `wrap`: more [wrap] keys."""
    from faultflow.cli import main

    if netlist is None:
        netlist = work / "chip.json"
        netlist.write_text(json.dumps(_ring()), encoding="utf-8")
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n[wrap]\nenabled = true\n{wrap}\n"
        f"[scan]\nchains = 1\n\n{sections}",
        encoding="utf-8",
    )
    patterns = work / "patterns.json"
    for step in (["init"], ["scan"], ["scan-check"]):
        assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    command = ["intest", "--export-patterns", str(patterns), "--top", top]
    assert main([*command, "-c", str(ofs)]) == 0
    return load_config(ofs, top), patterns


def _faults(cfg: Any) -> list[sqlite3.Row]:
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT fault_site_key, lower(fault_type) AS type, status, exclusion, "
            "blackbox_unresolved, collapsed_into FROM faults WHERE campaign_id = "
            "(SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan')"
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.integration
def test_intest_patterns_test_the_core_whatever_drives_the_block(
    tmp_path: Path, flow_tools: None
) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = _intest(tmp_path)
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
        faults = {(row["fault_site_key"], row["type"]): row for row in _faults(cfg)}
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
        cfg, patterns = _intest(
            tmp_path,
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
        faults = {(row["fault_site_key"], row["type"]): row for row in _faults(cfg)}
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


ACCUMULATOR = """\
module acc (input clk, input rst_n, input en, input [1:0] a, input [1:0] b,
            output [1:0] sum, output carry);
  reg [1:0] r;
  reg c;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      r <= 2'b00;
      c <= 1'b0;
    end else if (en)
      {c, r} <= r + a + b;
  assign sum = r ^ b;
  assign carry = c & en;
endmodule
"""


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
        cfg, patterns = _intest(
            tmp_path, fault_model, netlist=rtl, top="acc", wrap="exclude = b[1]\n"
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
