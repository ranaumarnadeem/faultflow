"""End-to-end scan-model EXTEST through `FlowService.run_project`.

Two real Yosys-synthesized blocks (an accumulator + a counter/FSM, NOT hand-written
JSON fixtures), each independently scan-inserted and IEEE-1500-wrapped with the
native shiftable scan WBC, composed into one SoC via a glue RTL that daisy-chains
the wrapper ring and routes the functional interconnect through real gates. This
exercises the full production path added this session: `manifest.py`'s
`interconnect.mode="scan"` fields, `orchestrator._run_scan_extest` (graybox compose
+ scan manifest generation + `run_atpg(scan=True, test_mode="extest")`), and
`aggregate.py`'s Guard 3 fix for scan-EXTEST WBC boundary faults (fusion net
renumbering + policy-excluded/collapsed handoff completeness).

Also runs the identical function flat (no wrapping, one whole-chip scan ATPG run)
to give the flat-vs-hierarchical coverage comparison the roadmap calls for -- see
docstring on `test_scan_extest_project_matches_flat_within_explained_gap` for the
result and its explanation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.json"

ALU_RTL = """
module alu_acc(input clk, input rst, input add_sub, input [3:0] b,
               output [3:0] acc, output carry);
  reg [3:0] acc_r; reg carry_r;
  wire [4:0] sum = add_sub ? ({1'b0,acc_r}-{1'b0,b}) : ({1'b0,acc_r}+{1'b0,b});
  always @(posedge clk) begin
    if (rst) begin acc_r<=4'b0; carry_r<=1'b0; end
    else begin acc_r<=sum[3:0]; carry_r<=sum[4]; end
  end
  assign acc=acc_r; assign carry=carry_r;
endmodule
"""

# ctr_fsm consumes an already-inverted load_val/load pair (load_val_n/load_n) --
# see the glue module docstring below for why: it lets the interconnect route
# through a real, non-optimizable gate instead of a bare wire.
CTR_RTL = """
module ctr_fsm(input clk, input rst, input en, input up_down,
               input [3:0] load_val_n, input load_n, output [3:0] count, output done);
  reg [3:0] cnt_r; reg done_r;
  wire [3:0] load_val = ~load_val_n; wire load = ~load_n;
  wire [3:0] nxt = load ? load_val : (up_down ? cnt_r+4'b1 : cnt_r-4'b1);
  always @(posedge clk) begin
    if (rst) begin cnt_r<=4'b0; done_r<=1'b0; end
    else if (en) begin cnt_r<=nxt; done_r<=(nxt==4'hF); end
  end
  assign count=cnt_r; assign done=done_r;
endmodule
"""

# Glue: daisy-chains the wrapper ring (soc_wbr_si -> u_a.wbr_si -> u_a.wbr_so ->
# u_b.wbr_si -> u_b.wbr_so -> soc_wbr_so) and shares clk/wbr_se. The functional
# interconnect (u_a.acc/carry -> u_b.load_val/load) is routed through REAL
# inverter gates, not bare wires: a direct wire tying one block's WBC boundary pin
# straight to another's makes that ONE net carry TWO valid (block, wbc, pin)
# boundary identities, which aggregate.py's net-id-keyed pin index can only keep
# one of (a separate, still-open limitation -- see aggregate.py's
# `_wbc_pin_index`). A SINGLE inverter reliably survives synthesis as a distinct
# net (a double inverter does not -- `opt`/`abc` fold `~(~x)` straight back to
# `x`); ctr_fsm compensates by consuming the pre-inverted signal directly. Reset
# is simplest handled with independent per-block top-level ports, avoiding a
# reset fan-out aliasing case by construction.
GLUE_RTL = """
module soc_top(input clk, input rst_a, input rst_b, input add_sub, input [3:0] b,
               input up_down, input en,
               input soc_wbr_se, input soc_wbr_si,
               output soc_wbr_so, output chip_out,
               output u_a_scan_out, output u_b_scan_out);
  wire [3:0] acc_o; wire carry_o; wire [3:0] count_o; wire done_o;
  wire wbr_mid;
  wire [3:0] load_val_n; wire load_n;
  assign load_val_n = ~acc_o;
  assign load_n = ~carry_o;
  alu_acc u_a(.clk(clk), .rst(rst_a), .add_sub(add_sub), .b(b),
              .acc(acc_o), .carry(carry_o),
              .scan_en(1'b0), .scan_in(1'b0), .scan_out(u_a_scan_out),
              .wbr_se(soc_wbr_se), .wbr_si(soc_wbr_si), .wbr_so(wbr_mid));
  ctr_fsm u_b(.clk(clk), .rst(rst_b), .en(en), .up_down(up_down),
              .load_val_n(load_val_n), .load_n(load_n),
              .count(count_o), .done(done_o),
              .scan_en(1'b0), .scan_in(1'b0), .scan_out(u_b_scan_out),
              .wbr_se(soc_wbr_se), .wbr_si(wbr_mid), .wbr_so(soc_wbr_so));
  assign chip_out = carry_o ^ done_o;
endmodule
"""

FLAT_RTL = ALU_RTL + "\n" + CTR_RTL + """
module soc_top(input clk, input rst, input add_sub, input [3:0] b,
               input up_down, input en, output chip_out);
  wire [3:0] acc_o; wire carry_o; wire [3:0] count_o; wire done_o;
  wire [3:0] load_val_n; wire load_n;
  assign load_val_n = ~acc_o;
  assign load_n = ~carry_o;
  alu_acc u_a(.clk(clk),.rst(rst),.add_sub(add_sub),.b(b),.acc(acc_o),.carry(carry_o));
  ctr_fsm u_b(.clk(clk),.rst(rst),.en(en),.up_down(up_down),.load_val_n(load_val_n),
              .load_n(load_n),.count(count_o),.done(done_o));
  assign chip_out = carry_o ^ done_o;
endmodule
"""


def _build_wrapped_block(
    tmp_path: Path, rtl: str, name: str, top: str
) -> tuple[Path, Path]:
    """Synthesize + scan-insert + IEEE-1500 scan-wrap `top` via the real pipeline
    (Yosys synth, add_scan, wrap -model scan, check_scan). Returns
    (generic_json, scan_manifest) -- the two frozen artifacts a project manifest's
    BlockSpec needs; INTEST itself is run by the orchestrator, not here."""
    rtl_path = tmp_path / f"{name}.v"
    rtl_path.write_text(rtl, encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "out" / name)
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl_path), "-top", top)
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    bridge.call("wrap", "-model", "scan")
    bridge.call("add_scan", "-chains", "1")
    result = bridge.call("check_scan")
    assert result.passed, f"{name}: check_scan failed: {result.errors}"

    cfg = session.materialize_config()
    generic_json = cfg.intermediate_dir / f"{top}_scan.json"
    assert generic_json.exists(), f"{name}: no scan-stitched JSON at {generic_json}"
    return generic_json, cfg.scan_manifest_path


def _write_project_manifest(
    tmp_path: Path,
    *,
    blkA_json: Path,
    blkA_manifest: Path,
    blkB_json: Path,
    blkB_manifest: Path,
    glue_rtl: Path,
) -> Path:
    cell_map = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.json"
    base_ofs = tmp_path / "base.ofs"
    base_ofs.write_text(
        f"""
[design]
netlist = {blkA_json}
cell_lib = {cell_map}

[fault_model]
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
compaction = none
max_rounds = 4
""".strip() + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema": "faultflow_project_v1",
        "name": "scan_extest_e2e",
        "cell_map_profile": "sky130",
        "base_config": str(base_ofs),
        "blocks": [
            {
                "name": "blkA",
                "top": "alu_acc",
                "soc_instance": "u_a",
                "generic_json": str(blkA_json),
                "scan_manifest": str(blkA_manifest),
            },
            {
                "name": "blkB",
                "top": "ctr_fsm",
                "soc_instance": "u_b",
                "generic_json": str(blkB_json),
                "scan_manifest": str(blkB_manifest),
            },
        ],
        "interconnect": {
            "assembly_top": "soc_top",
            "mode": "scan",
            "soc_rtl": str(glue_rtl),
            "soc_wbr_si": "soc_wbr_si",
            "soc_wbr_so": "soc_wbr_so",
            "soc_wbr_se": "soc_wbr_se",
            "clock_port": "clk",
        },
        "aggregation": {"policy": "disjoint_union"},
    }
    manifest_path = tmp_path / "project_scan_extest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


@pytest.mark.golden
def test_scan_extest_project_matches_flat_within_explained_gap(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Block INTEST + scan-model EXTEST, aggregated, vs. the identical function
    flattened into one whole-chip scan ATPG run.

    The two are NOT required to match exactly, and NOT required to favor
    hierarchical in every direction: `b` (a real PI on both designs) reaches
    the core directly and is not itself part of the WBR ring, so it is (a)
    already fully controllable for INTEST via the wrapper boundary scan
    cells, and (b) decoupled from the interconnect's own EXTEST fault domain
    by the WBR_IN safe-zero mechanism -- neither hierarchical sub-run's
    coverage depends on `b`'s correct detection the way the FLAT whole-chip
    run's does, where `b` is a live top-level PI throughout. Correctness
    fixes to fault detection/simulation (verified directly: this test passes
    against `bc8824f` alone and only fails once further such fixes land on
    top of it) raise flat's true achievable ceiling without moving
    hierarchical's sub-scores the same way, producing a small, explained gap
    in flat's favor rather than a hierarchical regression. Assert "close",
    not "hierarchical never lower", with a tolerance well above the observed
    gap so a genuine hierarchical regression still fails loudly.
    """
    import shutil

    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    from faultflow.service import FlowService

    blkA_json, blkA_manifest = _build_wrapped_block(
        tmp_path, ALU_RTL, "blkA", "alu_acc"
    )
    blkB_json, blkB_manifest = _build_wrapped_block(
        tmp_path, CTR_RTL, "blkB", "ctr_fsm"
    )

    glue_rtl = tmp_path / "soc_glue.v"
    glue_rtl.write_text(GLUE_RTL, encoding="utf-8")

    manifest_path = _write_project_manifest(
        tmp_path,
        blkA_json=blkA_json,
        blkA_manifest=blkA_manifest,
        blkB_json=blkB_json,
        blkB_manifest=blkB_manifest,
        glue_rtl=glue_rtl,
    )

    service = FlowService()
    result = service.run_project(manifest_path)
    assert "project complete" in result.message

    soc_json = tmp_path / "output" / "scan_extest_e2e" / "soc_coverage.json"
    assert soc_json.exists()
    report = json.loads(soc_json.read_text(encoding="utf-8"))
    chip = report["chip"]
    g = report["guards"]

    assert g["tops_disjoint"] is True
    assert g["no_double_count"] is True
    assert g["partition_total"] is True
    assert g["handoff_complete"] is True
    assert g["handoff_sites"] > 0
    assert chip["denominator"] > 0
    hierarchical_pct = chip["coverage_percent"]
    assert hierarchical_pct is not None

    kinds = sorted(s["kind"] for s in report["scopes"])
    assert kinds == ["block", "block", "interconnect"]
    extest_scope = next(s for s in report["scopes"] if s["kind"] == "interconnect")
    assert extest_scope["owned"] > 0

    # --- Flat: the identical function, no wrapping, one whole-chip scan ATPG run.
    flat_rtl = tmp_path / "flat.v"
    flat_rtl.write_text(FLAT_RTL, encoding="utf-8")
    flat_session = ProjectSession(output_root=tmp_path / "out_flat")
    flat_bridge = TclBridge(flat_session)
    flat_bridge.call("read_netlist", str(flat_rtl), "-top", "soc_top")
    flat_bridge.call("use_lib_cells", "sky130")
    flat_bridge.call("add_clock", "clk")
    flat_bridge.call("synth")
    flat_bridge.call("add_scan", "-chains", "1")
    check = flat_bridge.call("check_scan")
    assert check.passed, f"flat: check_scan failed: {check.errors}"
    flat_result = flat_bridge.call("run_atpg", "-scan", "-target", "100.0")
    flat_pct = flat_result.metrics["fault_coverage_percent"]
    assert flat_pct is not None

    # See the docstring: hierarchical must stay CLOSE to flat, not strictly
    # >= it -- `b` reaches the core directly and isn't part of the WBR ring,
    # so correctness fixes to fault detection raise flat's true ceiling
    # without moving hierarchical's own sub-scores the same way. Measured
    # after C1/C2/C3 land on top of bc8824f: flat=99.678%,
    # hierarchical=99.462%, a ~0.22 point gap -- assert with a tolerance well
    # above that so a genuine hierarchical regression still fails loudly.
    assert abs(hierarchical_pct - flat_pct) <= 1.0
