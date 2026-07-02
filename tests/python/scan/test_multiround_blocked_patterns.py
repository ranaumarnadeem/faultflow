"""Regression: scan ATPG must not crash with "blocked pattern length
mismatch" on a design whose top module has a multi-bit input port.

Root cause: the C++ SAT PI enumeration (``ordered_pis`` in
``src/core/atpg/fault_solver.cpp``) skipped any input port whose ``bits``
list had more than one entry -- the locked synth script never runs Yosys's
``splitnets``, so a Verilog port like ``input [3:0] b`` stays ONE Yosys JSON
port entry with 4 bits. That silently dropped the port from the SAT PI set
entirely.

Meanwhile the Python-side PI-name list used to size and persist
``blocked_patterns`` bit-strings (``faultflow.runner.runner._port_names``,
called without ``expand_buses``) counts a multi-bit port once regardless of
width -- the same "one name per port" convention
``ParsedGraph::net_id_by_name`` already uses to resolve a bus PI's stimulus
to its first bit. So the two PI enumerations disagreed in length by exactly
the number of multi-bit ports on the top module.

The mismatch is invisible on a fault's FIRST solve (no blocked patterns
yet), and only surfaces once a SAT candidate for that fault is rejected and
its pattern gets persisted to the ``blocked_patterns`` table
(``faultflow/db/candidates.py``) -- the next round's re-solve for the same
fault passes that persisted (Python-length) key into the C++ solver's
(shorter) PI list, tripping the ``blocked.size() != pis.size()`` guard in
every one of ``fault_solver.cpp``'s four solve entry points and raising
``RuntimeError: blocked pattern length mismatch`` from
``core.solve_fault_atpg``.

This is a plain flat-scan (no wrapper, no INTEST) reproduction: a top
instantiating two small sequential submodules, one of which has a 4-bit
input bus (``b``). Scan-inserted as one chain, default FUNCTIONAL scan.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

# Mirrors the style of tests/python/test_synth_multimodule.py's
# _MULTI_MODULE_RTL: a top instantiating two small sequential submodules,
# flattened by the locked synth script. `b` (alu_acc's operand) is a 4-bit
# bus input port -- the trigger for the PI-enumeration mismatch above.
_SOC_TOP_RTL = """
module alu_acc(
  input clk,
  input rst,
  input add_sub,
  input [3:0] b,
  output [3:0] acc,
  output carry
);
  reg [3:0] acc_r;
  reg carry_r;
  wire [4:0] sum = add_sub ? ({1'b0,acc_r} - {1'b0,b}) : ({1'b0,acc_r} + {1'b0,b});
  always @(posedge clk) begin
    if (rst) begin
      acc_r <= 4'b0;
      carry_r <= 1'b0;
    end else begin
      acc_r <= sum[3:0];
      carry_r <= sum[4];
    end
  end
  assign acc = acc_r;
  assign carry = carry_r;
endmodule

module ctr_fsm(
  input clk,
  input rst,
  input en,
  input up_down,
  input [3:0] load_val,
  input load,
  output [3:0] count,
  output done
);
  reg [3:0] cnt_r;
  reg done_r;
  wire [3:0] next = load ? load_val : (up_down ? cnt_r + 4'b1 : cnt_r - 4'b1);
  always @(posedge clk) begin
    if (rst) begin
      cnt_r <= 4'b0;
      done_r <= 1'b0;
    end else if (en) begin
      cnt_r <= next;
      done_r <= (next == 4'hF);
    end
  end
  assign count = cnt_r;
  assign done = done_r;
endmodule

module soc_top(
  input clk,
  input rst,
  input add_sub,
  input [3:0] b,
  input up_down,
  input en,
  output chip_out
);
  wire [3:0] acc_o;
  wire carry_o;
  wire [3:0] count_o;
  wire done_o;

  alu_acc u_a(.clk(clk), .rst(rst), .add_sub(add_sub), .b(b),
              .acc(acc_o), .carry(carry_o));
  ctr_fsm u_b(.clk(clk), .rst(rst), .en(en), .up_down(up_down),
              .load_val(acc_o), .load(carry_o), .count(count_o), .done(done_o));

  assign chip_out = carry_o ^ done_o;
endmodule
"""


def test_scan_atpg_survives_multibit_top_port_across_rounds(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """run_atpg -scan must complete (not raise) on a design with a 4-bit PI.

    Before the fix this raised `RuntimeError: blocked pattern length
    mismatch` from `core.solve_fault_atpg` once the progressive scan ATPG
    loop re-solved a fault whose earlier SAT candidate had been rejected and
    persisted as a blocked pattern.
    """
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "soc_top.v"
    rtl.write_text(_SOC_TOP_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "soc_top")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    bridge.call("add_scan", "-chains", "1")
    bridge.call("check_scan")

    result = bridge.call("run_atpg", "-scan", "-target", "100.0")

    message = str(result)
    assert "coverage=" in message
    metrics = getattr(result, "metrics", {})
    assert metrics.get("detected", 0) > 0
