"""Synthesis of the inserted fixture chip: the DFT frozen and nested, never
optimized with the user's logic, and the gate-level chip still right."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from faultflow.mbist.insert import InsertResult, mbist_insert
from faultflow.mbist.synth import SynthesizedChip, synthesize_inserted
from mbist_chip import CHIP, MODEL, ROOT, chip_copy
from warptap_helpers import skip_unless_warptap

LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
MEMORIES = ("core0_ram", "bank0_ram", "top_ram")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in ("yosys", "iverilog", "vvp")),
        reason="needs Yosys and Icarus Verilog on PATH",
    ),
]


def _synthesize(tmp: Path, result: InsertResult) -> SynthesizedChip:
    return synthesize_inserted(
        result.rtl,
        "chip_top",
        {m.instance: (m.shell, m.collar) for m in result.memories},
        libs=[CHIP / "macros/input_demo_8x16_scn4m.v"],
        liberty=LIBERTY,
        workdir=tmp / "synth",
        top_leaves=None if result.jtag is None else result.jtag.instances(),
    )


@pytest.fixture(scope="module")
def synthesized(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[InsertResult, SynthesizedChip]:
    tmp = tmp_path_factory.mktemp("synth")
    result = mbist_insert(chip_copy(tmp), "chip_top", out=tmp / "out")
    return result, _synthesize(tmp, result)


@pytest.fixture(scope="module")
def synthesized_jtag(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[InsertResult, SynthesizedChip]:
    """The chip inserted with jtag: the TAP and the network are DFT too."""
    skip_unless_warptap()
    tmp = tmp_path_factory.mktemp("synth_jtag")
    spec = chip_copy(tmp, lambda t: t.replace("jtag: false", "jtag: {tck_max_mhz: 10}"))
    result = mbist_insert(spec, "chip_top", out=tmp / "out")
    return result, _synthesize(tmp, result)


def _cells(netlist: dict, top: str) -> dict:
    cells: dict = netlist["modules"][top]["cells"]
    return cells


def test_the_memories_sit_inside_their_shells(
    synthesized: tuple[InsertResult, SynthesizedChip],
) -> None:
    _, chip = synthesized
    assert chip.memories == (
        "g_bank[0].u_mem__u_collar.u_sram",
        "g_bank[1].u_mem",
        "u_core0.u_mem__u_collar.u_sram",
        "u_core1.u_mem",
        "u_mem_top__u_collar.u_sram",
    )


@pytest.mark.parametrize("chip_fixture", ["synthesized", "synthesized_jtag"])
def test_every_frozen_block_is_its_standalone_netlist_cell_for_cell(
    chip_fixture: str, request: pytest.FixtureRequest
) -> None:
    """Nothing re-synthesized a frozen block: under each instance prefix the
    composed chip has exactly the standalone netlist's cells."""
    result, chip = request.getfixturevalue(chip_fixture)
    shells = {m.instance for m in result.memories}
    composed = _cells(json.loads(chip.composed_json.read_text("utf-8")), chip.top)
    assert chip.frozen, "no frozen blocks"
    for prefix, module in chip.frozen.items():
        standalone = json.loads(chip.standalone[module].read_text("utf-8"))
        own = _cells(standalone, module)
        spliced = {
            name[len(prefix) + 2 :]: cell
            for name, cell in composed.items()
            if name.startswith(prefix + "__")
        }
        if prefix not in shells:  # a leaf: its cells, and nothing nested under it
            assert spliced.keys() == own.keys(), prefix
        else:  # a shell: its own cells, its leaves' among them
            assert set(own) <= set(spliced), prefix
        assert Counter(c["type"] for k, c in spliced.items() if k in own) == Counter(
            c["type"] for c in own.values()
        ), prefix


def test_the_tap_and_the_network_are_frozen_blocks(
    synthesized_jtag: tuple[InsertResult, SynthesizedChip],
) -> None:
    """Every instance warptap added -- TAP, SIBs, TDR bits, the TDRs' clear
    synchronizer -- is synthesized alone and spliced in, not optimized with the
    user's logic."""
    result, chip = synthesized_jtag
    assert result.jtag is not None
    network = result.jtag.instances()
    assert {"tap_core", "sib_cell", "tck_reset_sync"} <= set(network.values())
    assert {cell: chip.frozen.get(cell) for cell in network} == network


def _gate_verilog(chip: SynthesizedChip, work: Path) -> Path:
    gate = work / "gate.v"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "yosys",
            "-q",
            "-p",
            f"read_liberty -lib {LIBERTY}; read_json {chip.composed_json}; "
            f"write_verilog -noattr {gate}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return gate


def _simulate(sources: list[Path], tb: str, work: Path) -> str:
    work.mkdir(parents=True, exist_ok=True)
    (work / "tb.v").write_text(tb, encoding="utf-8")
    binary = work / "sim.vvp"
    compiled = subprocess.run(
        ["iverilog", "-g2012", "-DFUNCTIONAL", "-o", str(binary), "-s", "tb"]
        + [str(work / "tb.v"), *map(str, sources)],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    return subprocess.run(
        ["vvp", "-n", str(binary)], check=True, capture_output=True, text=True
    ).stdout


def _ports(result: InsertResult, active: set[str]) -> tuple[str, str]:
    """(declarations, port connections) for every memory's test ports."""
    decls, conns = [], []
    for memory in result.memories:
        for port, top_port in memory.top_ports.items():
            if port in ("test_mode", "bist_start"):
                decls.append(f"    reg {top_port} = 1'b0;")
                source = top_port if memory.memory in active else "1'b0"
                conns.append(f"        .{top_port}({source}),")
            else:
                decls.append(f"    wire {top_port};")
                conns.append(f"        .{top_port}({top_port}),")
    return "\n".join(decls), "\n".join(conns)


HEAD = """\
`timescale 1ns/1ps
module tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst_n = 1'b0;
    reg [4:0] en = 5'b0;
    reg we = 1'b0;
    reg [3:0] addr = 4'b0;
    reg [7:0] wdata = 8'b0;
    wire [7:0] rdata_core0, rdata_core1, rdata_bank0, rdata_bank1, rdata_top_q;
"""

DUT = """
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{conns}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
"""


def _bist_tb(result: InsertResult, defparams: str = "") -> str:
    decls, conns = _ports(result, set(MEMORIES))
    done = " && ".join(f"{m}_bist_done" for m in MEMORIES)
    status = " ".join(f"{m}=%b%b" for m in MEMORIES)
    values = ", ".join(f"{m}_bist_done, {m}_bist_fail" for m in MEMORIES)
    sets = "\n".join(f"        {m}_test_mode = 1'b1;" for m in MEMORIES)
    starts = "\n".join(f"        {m}_bist_start = 1'b1;" for m in MEMORIES)
    return HEAD + decls + defparams + DUT.format(conns=conns) + f"""
    integer cycles = 0;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
        repeat (3) @(negedge clk);
{sets}
        repeat (4) @(negedge clk);
{starts}
        while (!({done}) && cycles < 2000) begin
            @(posedge clk);
            cycles = cycles + 1;
        end
        $display("RESULT {status}", {values});
        $finish;
    end
endmodule
"""


def test_the_gate_level_chip_runs_every_bist(
    synthesized: tuple[InsertResult, SynthesizedChip], tmp_path: Path
) -> None:
    result, chip = synthesized
    gate = _gate_verilog(chip, tmp_path)
    good = _simulate([gate, MODEL, SKY130_MODELS], _bist_tb(result), tmp_path / "good")
    assert "RESULT core0_ram=10 bank0_ram=10 top_ram=10" in good, good
    stuck = """
    defparam dut.\\u_mem_top__u_collar.u_sram .STUCK_ADDR = 9;
    defparam dut.\\u_mem_top__u_collar.u_sram .STUCK_BIT = 6;
    defparam dut.\\u_mem_top__u_collar.u_sram .STUCK_VALUE = 0;
"""
    bad = _simulate(
        [gate, MODEL, SKY130_MODELS], _bist_tb(result, stuck), tmp_path / "stuck"
    )
    assert "RESULT core0_ram=10 bank0_ram=10 top_ram=11" in bad, bad


def test_the_gate_level_chip_behaves_as_the_original_rtl(
    synthesized: tuple[InsertResult, SynthesizedChip], tmp_path: Path
) -> None:
    """With test_mode off, the synthesized chip's outputs equal the original
    RTL's on every cycle of random reads and writes."""
    result, chip = synthesized
    decls, conns = _ports(result, set())
    body = """
    integer i;
    integer seed = 1031;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
        for (i = 0; i < 400; i = i + 1) begin
            @(negedge clk);
            #4 $display("%0d %h %h %h %h %h", i, rdata_core0, rdata_core1,
                        rdata_bank0, rdata_bank1, rdata_top_q);
            en = $random(seed);
            we = $random(seed);
            addr = $random(seed);
            wdata = $random(seed);
        end
        $finish;
    end
endmodule
"""
    original = _simulate(
        [CHIP / "rtl/core.v", CHIP / "rtl/chip_top.v", MODEL],
        HEAD + DUT.format(conns="") + body,
        tmp_path / "rtl",
    )
    gate = _simulate(
        [_gate_verilog(chip, tmp_path / "g"), MODEL, SKY130_MODELS],
        HEAD + decls + DUT.format(conns=conns) + body,
        tmp_path / "gate",
    )

    def cycles(out: str) -> list[str]:
        return [line for line in out.splitlines() if re.match(r"\d+ ", line)]

    assert len(cycles(original)) == 400
    assert cycles(gate) == cycles(original)
    # The memories were exercised: their outputs carry data, not only x.
    assert len({line.split()[5] for line in cycles(original)}) > 10
