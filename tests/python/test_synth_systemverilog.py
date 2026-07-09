"""SystemVerilog (.sv) source support.

faultflow previously hard-rejected `.sv` sources at three separate checkpoints
(`session.read_netlist`, `runner._existing_verilog_source`,
`runner._find_verilog_source`) even though the fourth dispatch point
(`runner._find_netlist`) was already `.sv`-aware, and Yosys itself natively
supports the SystemVerilog subset these designs use (packages, `logic`,
`typedef enum`, `always_comb`/`always_ff`, `unique case`) via `read_verilog -sv`.

Multi-file designs (a shared package + several module files) are handled by
concatenating them into one `.sv` file before calling `read_netlist` -- Yosys
elaborates everything passed to one `read_verilog` invocation into a single
flattened netlist regardless of how many files it read, so faultflow itself
needs no multi-file source list.

One real Yosys 0.61 `-sv` limitation, found empirically while writing this
test: `import pkg::*;` (wildcard import) does not make the package's
`typedef`'d types resolvable at subsequent declaration sites, even though the
`import` statement itself parses without error -- only plain package
*parameters* survive a wildcard import. Fully-qualified references
(`pkg::type_name`, including at enum-literal use sites) work with no import
statement at all. Designs that rely on wildcard-imported types (as the real
RV32I benchmark RTL does) need their `import` lines stripped and type/literal
references qualified before being handed to faultflow -- a purely mechanical,
name-qualification-only rewrite, not a semantic one.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

import pytest

_SV_ALU_RTL = """
package tiny_pkg;
  typedef enum logic [1:0] {
    OP_AND,
    OP_OR,
    OP_XOR,
    OP_UNKNOWN
  } op_select_e;
endpackage

module tiny_sv_alu
(
    input  logic       clk_i,
    input  logic       rst_i,
    input  logic [3:0] a_i,
    input  logic [3:0] b_i,
    input  logic [1:0] op_i,
    output logic [3:0] result_o
);
    logic [3:0] alu_result;
    tiny_pkg::op_select_e op_sel;

    always_comb begin
        unique case (op_i)
            2'b00:   op_sel = tiny_pkg::OP_AND;
            2'b01:   op_sel = tiny_pkg::OP_OR;
            2'b10:   op_sel = tiny_pkg::OP_XOR;
            default: op_sel = tiny_pkg::OP_UNKNOWN;
        endcase
    end

    always_comb begin
        unique case (op_sel)
            tiny_pkg::OP_AND:  alu_result = a_i & b_i;
            tiny_pkg::OP_OR:   alu_result = a_i | b_i;
            tiny_pkg::OP_XOR:  alu_result = a_i ^ b_i;
            default: alu_result = 4'b0;
        endcase
    end

    always_ff @(posedge clk_i or posedge rst_i) begin
        if (rst_i)
            result_o <= 4'b0;
        else
            result_o <= alu_result;
    end
endmodule
"""

# Identical to test_synth_multimodule.py's fixture: plain Verilog-2001, no SV
# constructs at all. Used as a regression guard -- adding `-sv` to the synth
# template must not change what a plain-Verilog design synthesizes to.
_PLAIN_MULTI_MODULE_RTL = """
module leaf_a(input clk, input a, input b, output y);
  reg r;
  always @(posedge clk) r <= a & b;
  assign y = ~r;
endmodule

module leaf_b(input clk, input a, input b, output y);
  reg r;
  always @(posedge clk) r <= a | b;
  assign y = ~r;
endmodule

module top2(input clk, input a, input b, output y);
  wire ya, yb;
  leaf_a u_a(.clk(clk), .a(a), .b(b), .y(ya));
  leaf_b u_b(.clk(clk), .a(ya), .b(b), .y(yb));
  assign y = yb;
endmodule
"""

# Captured by running this exact fixture through the CURRENT (pre-SV-support)
# synth flow, before any implementation change -- see task #46/#47. If this
# ever needs to change, it means -sv altered parsing of plain Verilog-2001,
# which must be understood, not silently accepted.
_PLAIN_RTL_EXPECTED_CELL_TYPE_COUNTS = {
    "sky130_fd_sc_hd__clkinv_1": 1,
    "sky130_fd_sc_hd__nand2b_1": 1,
    "sky130_fd_sc_hd__and2_0": 1,
    "sky130_fd_sc_hd__dfxtp_1": 2,
}


def _cell_type_counts(json_path: Path, top: str) -> Counter[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    module = data["modules"][top]
    return Counter(cell["type"] for cell in module["cells"].values())


def test_read_netlist_accepts_systemverilog_source(tmp_path: Path) -> None:
    rtl = tmp_path / "tiny_sv_alu.sv"
    rtl.write_text(_SV_ALU_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "tiny_sv_alu")

    assert session.top == "tiny_sv_alu"
    assert session.source_kind == "verilog"
    assert not session.synthesized


def test_systemverilog_design_synthesizes_end_to_end(tmp_path: Path) -> None:
    """Package + import + logic + typedef enum + always_comb/always_ff +
    unique case -- the SV subset the real RV32I benchmark RTL uses."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "tiny_sv_alu.sv"
    rtl.write_text(_SV_ALU_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "tiny_sv_alu")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk_i")
    bridge.call("synth")

    json_path = tmp_path / "out/tiny_sv_alu/.faultflow/intermediate/tiny_sv_alu.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    module = data["modules"]["tiny_sv_alu"]
    assert module["cells"], "synthesized SV design has zero cells"
    scopeinfo_cells = [
        inst
        for inst, cell in module["cells"].items()
        if cell.get("type") == "$scopeinfo"
    ]
    assert scopeinfo_cells == []


def test_plain_verilog_synth_unaffected_by_sv_flag(tmp_path: Path) -> None:
    """Regression guard: adding -sv to the synth template must not change
    what a plain Verilog-2001 design (no SV constructs at all) synthesizes
    to. Golden counts captured from the pre-change template."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "top2.v"
    rtl.write_text(_PLAIN_MULTI_MODULE_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "top2")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")

    json_path = tmp_path / "out/top2/.faultflow/intermediate/top2.json"
    counts = _cell_type_counts(json_path, "top2")
    assert dict(counts) == _PLAIN_RTL_EXPECTED_CELL_TYPE_COUNTS, (
        f"plain-Verilog synth result changed after adding -sv: {dict(counts)} "
        f"!= {_PLAIN_RTL_EXPECTED_CELL_TYPE_COUNTS}"
    )
