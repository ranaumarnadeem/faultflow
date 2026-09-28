"""Gate-level acceptance test for the autoMBIST composition.

compose_soc's Yosys `check -assert` guard (autombist._check_composed_netlist_
drivers) catches a connection lost at an instance boundary -- an undriven or
multiply-driven net -- but not a connection made to the WRONG driven net:
wire the memory's addr0[0] to addr0[1]'s net and every net still has exactly
one driver. So simulate what the composition produced: the composed netlist,
written back out as Verilog and run with the PDK cell models and a behavioral
memory, must still run the BIST -- pass with a good memory, and fail with one
whose bit 0 reads back stuck at 0.

The same design wrapped for JTAG access (autoMBIST wrap-test-access) is run
through real JTAG: EXTEST loaded into the TAP, then test_mode=1 and
bist_start=1 written over the IJTAG network, with warptap's own PDL
retargeter producing the TMS/TDI stream. Wrapping makes those two control
ports JTAG-only, so driving their pins instead must not start the BIST.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.integrations.autombist import (
    AutombistManifest,
    AutombistTestAccess,
    _check_composed_netlist_drivers,
    load_autombist_manifest,
    synthesize_from_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
JTAG_FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m_jtag"
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


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, cwd=cwd)


def _simulate(
    composed_json: Path, work: Path, testbench: str, defines: list[str]
) -> tuple[str, str]:
    """(bist_done, bist_fail) that `testbench` reports for the composed
    netlist, simulated with the PDK cell models and the behavioral memory."""
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
    tb = work / "tb.v"
    tb.write_text(testbench, encoding="utf-8")
    sim = work / "sim.vvp"
    compiled = _run(
        ["iverilog", "-g2012", "-DFUNCTIONAL", *defines, "-o", str(sim)]
        + [str(p) for p in (tb, gate, memory, SKY130_MODELS)]
    )
    assert compiled.returncode == 0, compiled.stderr
    run = _run(["vvp", "-n", str(sim)], cwd=work)
    result = re.search(r"RESULT done=(\S+) fail=(\S+)", run.stdout)
    assert result is not None, run.stdout + run.stderr
    return result.group(1), result.group(2)


def _run_bist(composed_json: Path, work: Path, *, stuck_bit: bool) -> tuple[str, str]:
    """(bist_done, bist_fail) after simulating the composed netlist to done."""
    return _simulate(
        composed_json, work, TESTBENCH, ["-DSTUCK_BIT"] if stuck_bit else []
    )


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


# The wrapped design on two clocks: clk for the MBIST logic, tck for the TAP
# and its IJTAG network. The control pins stay 0, so the BIST can start only
# over JTAG -- unless PINS_ONLY drives them to 1 and plays no JTAG at all.
# jtag.mem holds one "<tms><tdi>" pair per TCK cycle, JTAG_CYCLES of them,
# played from Run-Test/Idle: warptap's to_cycles starts there.
JTAG_TESTBENCH = """\
`timescale 1ns/1ps
module tb;
    reg        clk = 1'b0;
    reg        rst_n = 1'b0;
    reg        tck = 1'b0;
    reg        tms = 1'b1;
    reg        tdi = 1'b0;
    reg        trst_n = 1'b0;
`ifdef PINS_ONLY
    reg        test_mode = 1'b1;
    reg        bist_start = 1'b1;
`else
    reg        test_mode = 1'b0;
    reg        bist_start = 1'b0;
`endif
    wire       bist_done;
    wire       bist_fail;
    wire       tdo;
    wire [7:0] func_dout;

    input_demo_8x16_scn4m_mbist dut (
        .clk(clk), .rst_n(rst_n), .test_mode(test_mode),
        .bist_start(bist_start), .bist_done(bist_done), .bist_fail(bist_fail),
        .func_csb(1'b1), .func_addr(4'd0), .func_din(8'd0), .func_we(1'b0),
        .func_dout(func_dout),
        .tck(tck), .tms(tms), .tdi(tdi), .trst_n(trst_n), .tdo(tdo)
    );

    always #10 clk = ~clk;

    task tck_cycle;
        begin
            #25 tck = 1'b1;
            #25 tck = 1'b0;
        end
    endtask

    reg [1:0] jtag_program [0:`JTAG_CYCLES - 1];
    integer i;
    integer cycles;
    initial begin
        $readmemb("jtag.mem", jtag_program);
        repeat (2) tck_cycle;
        trst_n = 1'b1;
        rst_n = 1'b1;
        tms = 1'b0;
        tck_cycle;
`ifndef PINS_ONLY
        for (i = 0; i < `JTAG_CYCLES; i = i + 1) begin
            {tms, tdi} = jtag_program[i];
            tck_cycle;
        end
`endif
        tms = 1'b0;
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
def wrapped(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AutombistTestAccess, Path]:
    manifest = load_autombist_manifest(JTAG_FIXTURE / "manifest.json")
    assert manifest.test_access is not None
    result = synthesize_from_manifest(
        manifest,
        out=tmp_path_factory.mktemp("gate_level_jtag") / "synth",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )
    return manifest.test_access, result.composed_json_path


def _jtag_program(access: AutombistTestAccess) -> list[tuple[int, int]]:
    """TMS/TDI per TCK cycle: load EXTEST, then write test_mode=1 and
    bist_start=1 through the IJTAG network the manifest describes."""
    if importlib.util.find_spec("warptap") is None:
        pytest.skip("needs warptap importable (e.g. PYTHONPATH=~/warptap/src)")
    # warptap is optional: without it, mypy finds no module to check against.
    from warptap.icl_model import (  # type: ignore[import-not-found]
        InstrumentDirection,
        SignalBinding,
        slot_name,
    )
    from warptap.pdl_interpreter import (  # type: ignore[import-not-found]
        PDLInterpreter,
    )
    from warptap.sib_plan import (  # type: ignore[import-not-found]
        InstrumentSpec,
        build_sib_plan,
    )
    from warptap.tap_fsm import TapState  # type: ignore[import-not-found]
    from warptap.tap_ir import GotoState, ShiftIR  # type: ignore[import-not-found]
    from warptap.tap_ir_play import to_cycles  # type: ignore[import-not-found]

    # The network wrap-test-access built, rebuilt from the manifest the way
    # autoMBIST built it: one SIB per instrument, in chain order.
    specs = [
        InstrumentSpec(
            ins.name,
            width=ins.width,
            capture_value=0,
            direction=(
                InstrumentDirection.WRITE
                if ins.role == "control"
                else InstrumentDirection.READ
            ),
            signal_bits=tuple(SignalBinding(ins.name, b) for b in range(ins.width)),
        )
        for ins in access.instruments
    ]
    graph, root = build_sib_plan(specs, top_name=access.top_module)
    sib_of = {i.hierarchical_path: i.sib_name for i in access.instances}
    assert [slot_name(node) for node in graph.chain] == [
        sib_of[ins.sib] for ins in access.instruments
    ], "the rebuilt IJTAG network is not the one in the netlist"

    pdl = PDLInterpreter(graph, root)
    for control in ("test_mode", "bist_start"):
        pdl.iTarget(control)
        pdl.iWrite(1)
        pdl.iApply()
    # warptap's TAP: a 4-bit IR, EXTEST = 0. Its IJTAG network shifts on
    # every DR scan whatever the instruction (the SIBs' select is tied high),
    # so EXTEST matters for reading the network at TDO; it is loaded first all
    # the same, as a real test would.
    ir_ops: list[Any] = [
        GotoState(TapState.SHIFT_IR),
        ShiftIR(4, tdi=0),
        GotoState(TapState.RUN_TEST_IDLE),
        *pdl.program,
    ]
    return [(int(tms), int(tdi)) for tms, tdi in to_cycles(ir_ops)]


def _run_jtag_bist(
    access: AutombistTestAccess,
    composed_json: Path,
    work: Path,
    *,
    stuck_bit: bool = False,
    pins_only: bool = False,
) -> tuple[str, str]:
    # PINS_ONLY plays no JTAG; its one-cycle program only sizes the array.
    program = [(0, 0)] if pins_only else _jtag_program(access)
    (work / "jtag.mem").write_text(
        "".join(f"{tms}{tdi}\n" for tms, tdi in program), encoding="utf-8"
    )
    defines = [f"-DJTAG_CYCLES={len(program)}"]
    if stuck_bit:
        defines.append("-DSTUCK_BIT")
    if pins_only:
        defines.append("-DPINS_ONLY")
    return _simulate(composed_json, work, JTAG_TESTBENCH, defines)


@pytest.mark.integration
@pytest.mark.parametrize(("stuck_bit", "expected_fail"), [(False, "0"), (True, "1")])
def test_wrapped_netlist_runs_the_bist_over_jtag(
    wrapped: tuple[AutombistTestAccess, Path],
    tmp_path: Path,
    stuck_bit: bool,
    expected_fail: str,
) -> None:
    access, composed_json = wrapped

    done, fail = _run_jtag_bist(access, composed_json, tmp_path, stuck_bit=stuck_bit)

    assert done == "1", "the BIST never finished"
    assert fail == expected_fail


@pytest.mark.integration
def test_wrapped_control_pins_no_longer_start_the_bist(
    wrapped: tuple[AutombistTestAccess, Path], tmp_path: Path
) -> None:
    """test_mode and bist_start driven at their pins, with no JTAG: wrapping
    left those pins without fanout, so the BIST never starts."""
    access, composed_json = wrapped

    done, _fail = _run_jtag_bist(access, composed_json, tmp_path, pins_only=True)

    assert done == "0"
