"""A faultflow_project_v2 SoC for the project tests: two blocks Yosys synthesizes (an
accumulator and a counter), each wrapped with the IEEE 1500 wrapper ff.py wrap puts
on and scanned on its own, and glue that strings their wrapper chains into one,
brings their core chains and mode pins to SoC pins, and routes the interconnect
through inverters.

The blocks' workspaces go under the working directory (``[design] output_root``'s
default): change into the test's directory first."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.config import load_config

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

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

GLUE_RTL = """
module soc_top(input clk, input rst_a, input rst_b, input add_sub, input [3:0] b,
               input up_down, input en, input scan_en, input si_a, input si_b,
               input wsi, input t_intest, input t_extest,
               output so_a, output so_b, output wso, output chip_out);
  wire [3:0] acc_o; wire carry_o; wire [3:0] count_o; wire done_o; wire w_mid;
  wire [3:0] load_val_n; wire load_n;
  assign load_val_n = ~acc_o;
  assign load_n = ~carry_o;
  alu_acc u_a(.clk(clk), .rst(rst_a), .add_sub(add_sub), .b(b),
              .acc(acc_o), .carry(carry_o),
              .scan_en(scan_en), .scan_in(si_a), .scan_out(so_a),
              .wbr_si(wsi), .wbr_so(w_mid),
              .wbr_intest(t_intest), .wbr_extest(t_extest));
  ctr_fsm u_b(.clk(clk), .rst(rst_b), .en(en), .up_down(up_down),
              .load_val_n(load_val_n), .load_n(load_n),
              .count(count_o), .done(done_o),
              .scan_en(scan_en), .scan_in(si_b), .scan_out(so_b),
              .wbr_si(w_mid), .wbr_so(wso),
              .wbr_intest(t_intest), .wbr_extest(t_extest));
  assign chip_out = carry_o ^ done_o;
endmodule
"""


def base_ofs(path: Path, netlist: Path, extra: str = "") -> Path:
    path.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n{extra}",
        encoding="utf-8",
    )
    return path


def scanned_block(work: Path, rtl: str, top: str) -> tuple[Path, Path]:
    """`top` synthesized, wrapped and scanned on its own (one core chain, one
    wrapper chain), its scan-check passed: its scan JSON and scan manifest."""
    from faultflow.cli import main

    work.mkdir(parents=True, exist_ok=True)
    source = work / f"{top}.v"
    source.write_text(rtl, encoding="utf-8")
    ofs = base_ofs(
        work / f"{top}.ofs",
        source,
        "[wrap]\nenabled = true\n\n[scan]\nchains = 1\nwrapper_chains = 1\n",
    )
    for step in ("init", "scan", "scan-check"):
        assert main([step, "--top", top, "-c", str(ofs)]) == 0, step
    cfg = load_config(ofs, top)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    return Path(manifest["generic_json"]).resolve(), cfg.scan_manifest_path.resolve()


def write_soc_v2(
    root: Path, name: str = "soc2", hold: dict[str, int] | None = None
) -> Path:
    """The two blocks built under `root`, the glue and the base config written
    there, and the project manifest naming them (the SoC inputs `hold` names held
    in the SoC's tests): its path."""
    a_json, a_manifest = scanned_block(root / "blkA", ALU_RTL, "alu_acc")
    b_json, b_manifest = scanned_block(root / "blkB", CTR_RTL, "ctr_fsm")
    glue = root / "soc_glue.v"
    glue.write_text(GLUE_RTL, encoding="utf-8")
    base_ofs(root / "config.ofs", glue)
    project: dict[str, Any] = {
        "schema": "faultflow_project_v2",
        "name": name,
        "blocks": [
            {
                "name": "blkA",
                "top": "alu_acc",
                "soc_instance": "u_a",
                "generic_json": str(a_json),
                "scan_manifest": str(a_manifest),
            },
            {
                "name": "blkB",
                "top": "ctr_fsm",
                "soc_instance": "u_b",
                "generic_json": str(b_json),
                "scan_manifest": str(b_manifest),
            },
        ],
        "soc": {"top": "soc_top", "rtl": "soc_glue.v", "hold": hold or {}},
    }
    path = root / "project.json"
    path.write_text(json.dumps(project), encoding="utf-8")
    return path
