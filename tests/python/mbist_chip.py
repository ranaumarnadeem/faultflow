"""The MBIST-insertion fixture chip (tests/fixtures/mbist_chip), copied for a
test with a stand-in for autoMBIST that copies the collar fixture autoMBIST
generated for the algorithm asked for: tests/fixtures/autombist/
input_demo_8x16_scn4m_march_x for march-x (from mbist/sram_demo_x.yml),
input_demo_8x16_scn4m otherwise (march-c, from mbist/sram_demo.yml)."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
CHIP = ROOT / "tests/fixtures/mbist_chip"
COLLAR = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
COLLAR_MARCH_X = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m_march_x"
MODEL = CHIP / "sim/input_demo_8x16_scn4m_model.v"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"


def chip_copy(
    tmp: Path,
    spec_edit: Callable[[str], str] = lambda text: text,
    rtl_edit: Callable[[Path], None] | None = None,
) -> Path:
    """The fixture chip in `tmp`, its insertion file running the stand-in for
    autoMBIST; `spec_edit` and `rtl_edit` change the copy. Returns the
    insertion file."""
    for rel in ("rtl", "macros", "mbist", "sim"):
        shutil.copytree(CHIP / rel, tmp / rel)
    fake = tmp / "fake_autombist.py"
    fake.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--out') + 1])\n"
        "algo = args[args.index('--algo') + 1] if '--algo' in args else 'march-c'\n"
        f"collar = {str(COLLAR_MARCH_X)!r} if algo == 'march-x' else {str(COLLAR)!r}\n"
        "shutil.copytree(collar, out / 'input_demo_8x16_scn4m')\n",
        encoding="utf-8",
    )
    text = (CHIP / "mbist.yml").read_text(encoding="utf-8")
    text = text.replace("jtag: false", f"jtag: false\nautombist_cmd: python3 {fake}")
    spec = tmp / "mbist.yml"
    spec.write_text(spec_edit(text), encoding="utf-8")
    if rtl_edit is not None:
        rtl_edit(tmp)
    return spec


# --- playing a BIST program's vectors -------------------------------------------------

# TCK half period in ns against clk's 10 ns period, by TCK:clk speed ratio. With the
# resets released at 25.3 ns, no rising TCK edge of these falls on a clk edge, where
# a synchronizer's sampling would be a race in simulation.
TCK_HALF = {"1:8": 40.0, "1:1": 5.0, "8:1": 0.625, "135:1": 0.037}

_PROGRAM_TB = """\
`timescale 1ns/1ps
module tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    // TRST and the chip reset asserted by an edge just after time 0, then held.
    reg rst_n = 1'b1;
    reg trst_n = 1'b1;
    initial begin #1 rst_n = 1'b0; trst_n = 1'b0; end
    reg [4:0] en = 5'b0;
    reg we = 1'b0;
    reg [3:0] addr = 4'b0;
    reg [7:0] wdata = 8'b0;
    wire [7:0] rdata_core0, rdata_core1, rdata_bank0, rdata_bank1, rdata_top_q;
    reg tck = 1'b0;
    reg tms = 1'b1;
    reg tdi = 1'b0;
    wire tdo;
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
        .tck(tck), .tms(tms), .tdi(tdi), .trst_n(trst_n), .tdo(tdo),
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
@DEFPARAMS@
@PROBES@
    integer fd, code, kind, a, b, c, d;
    integer failures = 0;
    initial begin
        fd = $fopen("program.vec", "r");
        #25.3;
        // Both resets released together, just after a clk edge; one TCK edge (TMS
        // low) into Run-Test/Idle.
        trst_n = 1'b1;
        rst_n = 1'b1;
        tms = 1'b0;
        #(`TCK_HALF) tck = 1'b1;
        #(`TCK_HALF) tck = 1'b0;
        while (!$feof(fd)) begin
            code = $fscanf(fd, "%d %d %d %d %d\\n", kind, a, b, c, d);
            if (code == 5 && kind == 0) begin
                tms = a;
                tdi = b;
                #(`TCK_HALF);
                if (d != 0 && tdo !== c) begin
                    failures = failures + 1;
                    $display("MISMATCH read=%0d expected=%0d got=%b", d, c, tdo);
                end
                tck = 1'b1;
                #(`TCK_HALF) tck = 1'b0;
            end else if (code == 5) begin
                repeat (a) @(posedge clk);
                #1;
            end
        end
        $display("RESULT failures=%0d", failures);
@REPORT@
        $finish;
    end
endmodule
"""


@dataclass
class Played:
    failures: int
    mismatches: list[int]  # the reads that failed, 1-based
    lengths: dict[str, int]  # each probed controller's BIST length, in clk cycles
    out: str


def play_jtag_program(
    sources: list[Path],
    vectors: str,
    work: Path,
    *,
    ratio: str = "1:1",
    defparams: str = "",
    probes: dict[str, str] | None = None,
    defines: tuple[str, ...] = (),
) -> Played:
    """Play a BIST program's vectors (one line each, as mbist-insert writes them)
    against the fixture chip in `sources`: TRST and the chip reset first, then
    every TCK period and clock run, TDO checked where a read says. `probes` maps
    a memory to its collar's instance (RTL only): each controller's BIST length
    is measured, in clk edges started and not done."""
    lengths = "\n".join(
        f"    integer len_{m} = 0;\n    always @(posedge clk)\n"
        f"        if ({c}.bist_start === 1'b1 && {c}.bist_done === 1'b0)\n"
        f"            len_{m} = len_{m} + 1;"
        for m, c in (probes or {}).items()
    )
    report = (
        '        $display("LENGTH {}", {});'.format(
            " ".join(f"{m}=%0d" for m in probes),
            ", ".join(f"len_{m}" for m in probes),
        )
        if probes
        else ""
    )
    work.mkdir(parents=True, exist_ok=True)
    (work / "program.vec").write_text(vectors, encoding="utf-8")
    tb = (
        _PROGRAM_TB.replace("@DEFPARAMS@", defparams)
        .replace("@PROBES@", lengths)
        .replace("@REPORT@", report)
    )
    (work / "tb.v").write_text(tb, encoding="utf-8")
    binary = work / "sim.vvp"
    compiled = subprocess.run(
        ["iverilog", "-g2012", f"-DTCK_HALF={TCK_HALF[ratio]}", *defines]
        + ["-o", str(binary), "-s", "tb", str(work / "tb.v"), *map(str, sources)],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    out = subprocess.run(
        ["vvp", "-n", str(binary)], check=True, capture_output=True, text=True, cwd=work
    ).stdout
    result = re.search(r"RESULT failures=(\d+)", out)
    assert result, out
    found = re.search(r"LENGTH (.*)", out)
    return Played(
        int(result.group(1)),
        [int(r) for r in re.findall(r"MISMATCH read=(\d+)", out)],
        (
            {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", found.group(1))}
            if found
            else {}
        ),
        out,
    )


def gate_verilog(composed_json: Path, work: Path) -> Path:
    """A synthesized (composed) chip netlist as gate-level Verilog, in `work`."""
    gate = work / "gate.v"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "yosys",
            "-q",
            "-p",
            f"read_liberty -lib {LIBERTY}; read_json {composed_json}; "
            f"write_verilog -noattr {gate}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return gate
