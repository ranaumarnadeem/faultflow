"""Regression: synthesizing multi-module RTL must not leak Yosys `$scopeinfo`
cells into the output JSON.

`flatten` (in the locked synth script) inlines submodule instances but Yosys
leaves behind zero-port, zero-connection `$scopeinfo` markers recording where
each instance used to be (pure debug provenance, no functional meaning). Every
existing test fixture is single-module, so this was never exercised; a
multi-module design (a top instantiating two submodules) reproduces it. These
cells have no connections and can never carry a fault, but faultflow's
unsupported-cell policy (correctly) hard-fails on any cell type it doesn't
recognize -- so an unstripped `$scopeinfo` cell aborts `scan-check` with
"Unsupported cell: $scopeinfo" on any hierarchical RTL design.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

import pytest

_MULTI_MODULE_RTL = """
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


def test_synth_of_multimodule_rtl_has_no_scopeinfo_cells(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "top2.v"
    rtl.write_text(_MULTI_MODULE_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "top2")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")

    json_path = tmp_path / "out/top2/.faultflow/intermediate/top2.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    module = data["modules"]["top2"]
    scopeinfo_cells = [
        inst
        for inst, cell in module["cells"].items()
        if cell.get("type") == "$scopeinfo"
    ]
    assert scopeinfo_cells == [], (
        f"synthesized JSON leaked $scopeinfo cells: {scopeinfo_cells} "
        "(flatten's per-instance debug markers must be deleted before write_json)"
    )


def test_scan_check_succeeds_on_multimodule_rtl(tmp_path: Path) -> None:
    """End-to-end: the exact failure this bug caused -- scan-check aborting with
    'Unsupported cell: $scopeinfo' on any hierarchical (multi-module) design."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "top2.v"
    rtl.write_text(_MULTI_MODULE_RTL, encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "top2")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    bridge.call("add_scan", "-chains", "1")
    result = bridge.call("check_scan")  # must not raise ShellError
    assert "PASS" in str(result)
