"""Integrated clock gates (faultflow.scan.clock_gates): flops a design gates with
the library's clock gates scan on their root clock, every gate opened in shift by
its test enable -- the scan enable, which scan insertion connects to a gate that
ties it off and to the library's gate with one it puts in place of a gate without
-- and in capture a closed gate holds its flops, so the enable logic is graded. The
patterns replay on the gates' own cell models, and every fault they credit is
detected in the full protocol, the gates' latches simulated."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from scan_credit import credit_not_reproduced
from scan_replay import replay_on_cells

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

GATED = """module gated (input clk, input a, input b, input c, output y);
  reg r0, r1, r2, r3;
  wire gclk, gclk2, en;
  // A gate without a test enable, its enable from a flop; one that ties it off.
  assign en = a | r3;
  sky130_fd_sc_hd__dlclkp_1 u_cg (.CLK(clk), .GATE(en), .GCLK(gclk));
  sky130_fd_sc_hd__sdlclkp_1 u_cg2 (.CLK(clk), .GATE(c), .SCE(1'b0), .GCLK(gclk2));
  always @(posedge gclk) begin
    r0 <= a ^ r1;
    r1 <= r0 & b;
  end
  always @(posedge gclk2) r2 <= r0 | r3;
  always @(posedge clk) r3 <= r2 ^ a;
  assign y = r1 ^ r2 ^ r3;
endmodule
"""


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")


def _flow(work: Path, sections: str = "") -> tuple[Any, Path, Path]:
    from faultflow.cli import main

    rtl = work / "gated.v"
    rtl.write_text(GATED, encoding="utf-8")
    ofs = work / "gated.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {rtl}\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        f"[scan]\nchains = 1\n\n{sections}",
        encoding="utf-8",
    )
    patterns = work / "patterns.json"
    for step in (
        ["init"],
        ["scan"],
        ["scan-check"],
        ["sim", "--scan", "--export-patterns", str(patterns)],
    ):
        assert main([*step, "--top", "gated", "-c", str(ofs)]) == 0, step
    return load_config(ofs, "gated"), ofs, patterns


def _faults(cfg: Any) -> dict[str, str]:
    import sqlite3

    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT fault_site_key || ':' || lower(fault_type), status FROM faults "
            "WHERE campaign_id = (SELECT MAX(id) FROM campaigns)"
        ).fetchall()
    finally:
        conn.close()
    return {str(k): str(s) for k, s in rows}


@pytest.mark.integration
def test_gated_flops_scan_on_their_root_clock_and_their_gates_open_in_shift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(tmp_path)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    # Four flops, one root clock: one chain, no flop left out.
    assert manifest["cell_count"] == 4 and not manifest["ineligible_ffs"]
    assert len(manifest["clock_nets"]) == 1
    gated = {c["instance"]: c for c in manifest["cells"] if c.get("clock_gates")}
    assert len(gated) == 3
    assert {tuple(c["clock_gates"]) for c in gated.values()} == {
        ("u_cg",),
        ("u_cg2",),
    }
    # The gate without a test enable became sdlclkp, the scan enable on its SCE;
    # the tied-off one got the scan enable too.
    generic = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    cells = generic["modules"]["gated"]["cells"]
    se = generic["modules"]["gated"]["ports"][manifest["scan_enable"]]["bits"]
    assert cells["u_cg"]["type"] == "sky130_fd_sc_hd__sdlclkp_1"
    assert cells["u_cg"]["connections"]["SCE"] == se
    assert cells["u_cg2"]["connections"]["SCE"] == se
    assert manifest["clock_gates"]["u_cg"]["test_pin"] == "SCE"

    # Capture holds a closed gate's flops: the enable logic is graded -- each
    # gate's enable net (its only reader the gate: the stem's faults).
    faults = _faults(cfg)
    for gate in ("u_cg", "u_cg2"):
        (net,) = cells[gate]["connections"]["GATE"]
        site = {k: s for k, s in faults.items() if k.startswith(f"net:{net}:stem:")}
        assert len(site) == 2 and set(site.values()) == {"detected"}, (gate, site)

    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns) == []


@pytest.mark.integration
def test_gated_flops_transition_patterns_hold_in_the_full_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(
        tmp_path,
        "[fault_model]\nmodel = transition\nlaunch = loc\ncollapsing = false\n",
    )
    assert json.loads(patterns.read_text(encoding="utf-8"))
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns, loc=True) == []


@pytest.mark.integration
def test_a_clock_gate_scan_cant_open_in_shift_is_refused_by_scan_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A test enable the netlist drives from a functional input isn't scan
    insertion's to rewire; scan-check refuses it unless it is held."""
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    rtl = tmp_path / "gated.v"
    rtl.write_text(re.sub(r"\.SCE\(1'b0\)", ".SCE(b)", GATED), encoding="utf-8")
    ofs = tmp_path / "gated.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {rtl}\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        "[scan]\nchains = 1\n",
        encoding="utf-8",
    )
    for step in (["init"], ["scan"]):
        assert main([*step, "--top", "gated", "-c", str(ofs)]) == 0, step
    with pytest.raises(SystemExit):
        main(["scan-check", "--top", "gated", "-c", str(ofs)])
    err = capsys.readouterr().err
    assert "clock gates of scan flops that aren't held open" in err
    assert "SCE of u_cg2" in err and "the input b" in err
    # Held, it passes.
    ofs.write_text(ofs.read_text(encoding="utf-8") + "hold = b:1\n", encoding="utf-8")
    assert main(["scan", "--top", "gated", "-c", str(ofs)]) == 0
    assert main(["scan-check", "--top", "gated", "-c", str(ofs)]) == 0
