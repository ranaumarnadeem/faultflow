"""Gate-level acceptance test for the autoMBIST composition.

compose_soc's Yosys `check -assert` guard (autombist._check_composed_netlist_
drivers) catches a connection lost at an instance boundary -- an undriven or
multiply-driven net -- but not a connection made to the WRONG driven net:
wire the memory's addr0[0] to addr0[1]'s net and every net still has exactly
one driver. So simulate what the composition produced: the composed netlist,
written back out as Verilog and run with the PDK cell models and a behavioral
memory, must still run the BIST -- pass with a good memory, and fail with one
whose bit 0 reads back stuck at 0.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from faultflow.integrations.autombist import (
    AutombistManifest,
    _check_composed_netlist_drivers,
    load_autombist_manifest,
    synthesize_from_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
SKY130_MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("yosys", "iverilog", "vvp")),
    reason="needs Yosys and Icarus Verilog on PATH",
)

# Same timing as autoMBIST's own OpenRAM-style model of this memory: inputs
# registered on the rising edge, the write and the read done on the falling
# edge, so read data is valid well before the next rising edge -- where the
# READ_LATENCY=0 controller samples it. STUCK_BIT reads bit 0 back as 0.
MEMORY_MODEL = """\
`timescale 1ns/1ps
module input_demo_8x16_scn4m #(
    parameter integer ADDR_WIDTH = 4,
    parameter integer DATA_WIDTH = 8,
    parameter integer NUM_SPARE_ROWS = 0,
    parameter integer NUM_SPARE_COLS = 0
) (
    input  wire                  clk0,
    input  wire                  csb0,
    input  wire                  web0,
    input  wire [ADDR_WIDTH-1:0] addr0,
    input  wire [DATA_WIDTH-1:0] din0,
    output reg  [DATA_WIDTH-1:0] dout0
);
    reg [DATA_WIDTH-1:0] mem [0:(1 << ADDR_WIDTH) - 1];
    reg                  csb0_q;
    reg                  web0_q;
    reg [ADDR_WIDTH-1:0] addr0_q;
    reg [DATA_WIDTH-1:0] din0_q;

    always @(posedge clk0) begin
        csb0_q  <= csb0;
        web0_q  <= web0;
        addr0_q <= addr0;
        din0_q  <= din0;
    end

    always @(negedge clk0) begin
        if (!csb0_q && !web0_q)
            mem[addr0_q] <= din0_q;
        if (!csb0_q && web0_q)
`ifdef STUCK_BIT
            dout0 <= mem[addr0_q] & ~{{(DATA_WIDTH - 1){1'b0}}, 1'b1};
`else
            dout0 <= mem[addr0_q];
`endif
    end
endmodule
"""

TESTBENCH = """\
`timescale 1ns/1ps
module tb;
    reg        clk = 1'b0;
    reg        rst_n = 1'b0;
    reg        test_mode = 1'b1;
    reg        bist_start = 1'b0;
    wire       bist_done;
    wire       bist_fail;
    wire [7:0] func_dout;

    input_demo_8x16_scn4m_mbist dut (
        .clk(clk), .rst_n(rst_n), .test_mode(test_mode),
        .bist_start(bist_start), .bist_done(bist_done), .bist_fail(bist_fail),
        .func_csb(1'b1), .func_addr(4'd0), .func_din(8'd0), .func_we(1'b0),
        .func_dout(func_dout)
    );

    always #10 clk = ~clk;

    integer cycles;
    initial begin
        repeat (4) @(posedge clk);
        rst_n = 1'b1;
        @(posedge clk);
        bist_start = 1'b1;
        cycles = 0;
        while (bist_done !== 1'b1 && cycles < 20000) begin
            @(posedge clk);
            cycles = cycles + 1;
        end
        $display("RESULT done=%b fail=%b", bist_done, bist_fail);
        $finish;
    end
endmodule
"""


@pytest.fixture(scope="module")
def composed(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AutombistManifest, Path]:
    manifest = load_autombist_manifest(FIXTURE / "manifest.json")
    result = synthesize_from_manifest(
        manifest,
        out=tmp_path_factory.mktemp("gate_level") / "synth",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )
    return manifest, result.composed_json_path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _run_bist(composed_json: Path, work: Path, *, stuck_bit: bool) -> tuple[str, str]:
    """(bist_done, bist_fail) after simulating the composed netlist to done."""
    gate = work / "gate.v"
    written = _run(
        [
            "yosys",
            "-q",
            "-p",
            f"read_liberty -lib {SKY130_LIBERTY}; read_json {composed_json}; "
            f"write_verilog -noattr {gate}",
        ]
    )
    assert written.returncode == 0, written.stdout + written.stderr
    memory = work / "memory.v"
    memory.write_text(MEMORY_MODEL, encoding="utf-8")
    testbench = work / "tb.v"
    testbench.write_text(TESTBENCH, encoding="utf-8")
    sim = work / "sim.vvp"
    defines = ["-DFUNCTIONAL", *(["-DSTUCK_BIT"] if stuck_bit else [])]
    compiled = _run(
        ["iverilog", "-g2012", *defines, "-o", str(sim)]
        + [str(p) for p in (testbench, gate, memory, SKY130_MODELS)]
    )
    assert compiled.returncode == 0, compiled.stderr
    run = _run(["vvp", "-n", str(sim)])
    result = re.search(r"RESULT done=(\S+) fail=(\S+)", run.stdout)
    assert result is not None, run.stdout + run.stderr
    return result.group(1), result.group(2)


@pytest.mark.integration
@pytest.mark.parametrize(("stuck_bit", "expected_fail"), [(False, "0"), (True, "1")])
def test_composed_netlist_runs_the_bist(
    composed: tuple[AutombistManifest, Path],
    tmp_path: Path,
    stuck_bit: bool,
    expected_fail: str,
) -> None:
    _manifest, composed_json = composed

    done, fail = _run_bist(composed_json, tmp_path, stuck_bit=stuck_bit)

    assert done == "1", "the BIST never finished"
    assert fail == expected_fail


@pytest.mark.integration
def test_a_wrong_but_driven_connection_fails_the_bist(
    composed: tuple[AutombistManifest, Path], tmp_path: Path
) -> None:
    """The class of bug this test exists for: the memory's addr0[0] wired to
    addr0[1]'s net. Every net keeps exactly one driver, so the driver check
    finds nothing -- but two addresses now alias, and the BIST fails even
    with a good memory."""
    manifest, composed_json = composed
    data = json.loads(composed_json.read_text(encoding="utf-8"))
    addr = data["modules"][manifest.top_module]["cells"]["u_sram"]["connections"][
        "addr0"
    ]
    addr[0] = addr[1]
    mutated = tmp_path / "mutated.json"
    mutated.write_text(json.dumps(data), encoding="utf-8")

    _check_composed_netlist_drivers(
        manifest, mutated, liberty=SKY130_LIBERTY, workdir=tmp_path
    )  # must not raise: the driver check can't see this
    done, fail = _run_bist(mutated, tmp_path, stuck_bit=False)

    assert (done, fail) == ("1", "1")
