"""mbist-insert on the fixture chip, checked in simulation (Icarus Verilog):
with test_mode off the chip behaves exactly as before, and each memory's BIST
passes on a good memory and fails on a bad one.

autoMBIST itself isn't run: a stand-in copies the collar fixture
(tests/fixtures/autombist/input_demo_8x16_scn4m), generated from the same
config the fixture's insertion file names."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from faultflow.mbist.insert import InsertResult, mbist_insert
from mbist_chip import CHIP, MODEL, chip_copy

MEMORIES = ("core0_ram", "bank0_ram", "top_ram")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("yosys") is None or shutil.which("iverilog") is None,
        reason="needs Yosys and Icarus Verilog on PATH",
    ),
]


@pytest.fixture(scope="module")
def inserted(tmp_path_factory: pytest.TempPathFactory) -> InsertResult:
    tmp = tmp_path_factory.mktemp("insert")
    return mbist_insert(chip_copy(tmp), "chip_top", out=tmp / "out")


def _simulate(sources: list[Path], tb: str, workdir: Path) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    tb_path = workdir / "tb.v"
    tb_path.write_text(tb, encoding="utf-8")
    binary = workdir / "sim.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-o", str(binary), "-s", "tb", str(tb_path)]
        + [str(s) for s in sources],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["vvp", "-n", str(binary)], check=True, capture_output=True, text=True
    ).stdout


def _test_port_connections(result: InsertResult, *, active: set[str]) -> str:
    """Port connections for every memory's test ports: the control inputs of
    the `active` memories from tb regs <memory>_<port>, the rest tied off; the
    status outputs to tb wires."""
    lines = []
    for memory in result.memories:
        for port, top_port in memory.top_ports.items():
            if port in ("test_mode", "bist_start"):
                source = top_port if memory.memory in active else "1'b0"
                lines.append(f"        .{top_port}({source}),")
            else:
                lines.append(f"        .{top_port}({top_port}),")
    return "\n".join(lines)


def _declarations(result: InsertResult) -> str:
    lines = []
    for memory in result.memories:
        for port, top_port in memory.top_ports.items():
            kind = "reg " if port in ("test_mode", "bist_start") else "wire"
            init = " = 1'b0" if kind == "reg " else ""
            lines.append(f"    {kind} {top_port}{init};")
    return "\n".join(lines)


TB_HEAD = """\
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


def _equivalence_tb(test_ports: str = "", declarations: str = "") -> str:
    return TB_HEAD + declarations + f"""
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{test_ports}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
    integer i;
    integer seed = 20261001;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
        for (i = 0; i < 600; i = i + 1) begin
            @(negedge clk);
            #4 $display("%0d %h %h %h %h %h", i, rdata_core0, rdata_core1,
                        rdata_bank0, rdata_bank1, rdata_top_q);
            en = $random(seed);
            we = $random(seed);
            addr = $random(seed);
            wdata = $random(seed);
            if (i == 300) rst_n = 1'b0;
            if (i == 303) rst_n = 1'b1;
        end
        $finish;
    end
endmodule
"""


def test_with_test_mode_off_the_chip_behaves_as_before(
    inserted: InsertResult, tmp_path: Path
) -> None:
    """Random reads and writes on every memory, a chip reset in the middle: the
    inserted chip's outputs equal the original's on every cycle."""
    original = _simulate(
        [CHIP / "rtl/core.v", CHIP / "rtl/chip_top.v", MODEL],
        _equivalence_tb(),
        tmp_path / "original",
    )
    after = _simulate(
        [inserted.rtl, MODEL],
        _equivalence_tb(
            _test_port_connections(inserted, active=set()), _declarations(inserted)
        ),
        tmp_path / "inserted",
    )

    def cycles(out: str) -> list[str]:
        return [line for line in out.splitlines() if re.match(r"\d+ ", line)]

    assert len(cycles(original)) == 600
    assert cycles(after) == cycles(original)
    # The memories were exercised: their outputs carry data, not only x.
    assert len({line.split()[5] for line in cycles(original)}) > 10


def _bist_tb(result: InsertResult, defparams: str = "") -> str:
    status = " ".join(f"{m}_bist_done=%b {m}_bist_fail=%b" for m in MEMORIES)
    values = ", ".join(f"{m}_bist_done, {m}_bist_fail" for m in MEMORIES)
    starts = "\n".join(f"        {m}_test_mode = 1'b1;" for m in MEMORIES)
    go = "\n".join(f"        {m}_bist_start = 1'b1;" for m in MEMORIES)
    done = " && ".join(f"{m}_bist_done" for m in MEMORIES)
    return TB_HEAD + _declarations(result) + defparams + f"""
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{_test_port_connections(result, active=set(MEMORIES))}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
    integer cycles = 0;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
        repeat (3) @(negedge clk);
{starts}
        repeat (4) @(negedge clk);
{go}
        while (!({done}) && cycles < 2000) begin
            @(posedge clk);
            cycles = cycles + 1;
        end
        $display("done_after=%0d reads=%0d", cycles,
                 dut.u_mem_top.u_collar.u_sram.reads);
        $display("{status}", {values});
        $finish;
    end
endmodule
"""


def _results(out: str) -> dict[str, tuple[str, str]]:
    return {
        m: (
            re.search(rf"{m}_bist_done=(\S)", out).group(1),  # type: ignore[union-attr]
            re.search(rf"{m}_bist_fail=(\S)", out).group(1),  # type: ignore[union-attr]
        )
        for m in MEMORIES
    }


def test_every_bist_passes_on_good_memories(
    inserted: InsertResult, tmp_path: Path
) -> None:
    out = _simulate([inserted.rtl, MODEL], _bist_tb(inserted), tmp_path)
    assert _results(out) == {m: ("1", "0") for m in MEMORIES}, out


def test_a_stuck_bit_fails_only_its_own_memory(
    inserted: InsertResult, tmp_path: Path
) -> None:
    stuck = """
    defparam dut.u_core0.u_mem.u_collar.u_sram.STUCK_ADDR = 5;
    defparam dut.u_core0.u_mem.u_collar.u_sram.STUCK_BIT = 3;
    defparam dut.u_core0.u_mem.u_collar.u_sram.STUCK_VALUE = 1;
"""
    out = _simulate([inserted.rtl, MODEL], _bist_tb(inserted, stuck), tmp_path)
    assert _results(out) == {
        "core0_ram": ("1", "1"),
        "bank0_ram": ("1", "0"),
        "top_ram": ("1", "0"),
    }, out


def test_a_fault_on_the_last_compare_is_reported_when_done_rises(
    inserted: InsertResult, tmp_path: Path
) -> None:
    """autoMBIST raises its fail flag on the edge it enters done: the shell's
    done delay makes fail final by the time done rises, so a tester that
    reads both when it sees done can't miss a fault in the last compare."""
    passing = _simulate([inserted.rtl, MODEL], _bist_tb(inserted), tmp_path / "pass")
    reads = int(re.search(r"reads=(\d+)", passing).group(1))  # type: ignore[union-attr]
    last = f"""
    defparam dut.u_mem_top.u_collar.u_sram.FAIL_ON_READ = {reads};
"""
    out = _simulate([inserted.rtl, MODEL], _bist_tb(inserted, last), tmp_path / "last")
    assert _results(out)["top_ram"] == ("1", "1"), out


def test_the_reset_and_control_synchronizers_take_two_clock_edges(
    inserted: InsertResult, tmp_path: Path
) -> None:
    tb = TB_HEAD + _declarations(inserted) + f"""
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{_test_port_connections(inserted, active={"top_ram"})}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
    integer edges;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
        edges = 0;
        while (!dut.u_mem_top.rst_n_sync) begin
            @(posedge clk);
            #1 edges = edges + 1;
        end
        $display("reset_release_edges=%0d", edges);
        repeat (3) @(negedge clk);
        top_ram_test_mode = 1'b1;
        edges = 0;
        while (!dut.u_mem_top.synced_test_mode) begin
            @(posedge clk);
            #1 edges = edges + 1;
        end
        $display("control_edges=%0d", edges);
        rst_n = 1'b0;
        #1 $display("reset_assert_sync=%b test_mode_sync=%b",
                    dut.u_mem_top.rst_n_sync, dut.u_mem_top.synced_test_mode);
        $finish;
    end
endmodule
"""
    out = _simulate([inserted.rtl, MODEL], tb, tmp_path)
    assert "reset_release_edges=2" in out, out
    assert "control_edges=2" in out, out
    # The chip reset clears both at once, without a clock.
    assert "reset_assert_sync=0 test_mode_sync=0" in out, out


def test_an_active_high_reset_reaches_the_shells_inverted(tmp_path: Path) -> None:
    spec = chip_copy(tmp_path, lambda text: text.replace("active: low", "active: high"))
    result = mbist_insert(spec, "chip_top", out=tmp_path / "out")
    tb = TB_HEAD + _declarations(result) + f"""
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{_test_port_connections(result, active=set())}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
    initial begin
        rst_n = 1'b1;  // active high: the shells are in reset
        repeat (4) @(negedge clk);
        $display("held=%b%b", dut.u_mem_top.rst_n_sync,
                 dut.u_core0.u_mem.rst_n_sync);
        rst_n = 1'b0;
        repeat (3) @(negedge clk);
        $display("released=%b%b", dut.u_mem_top.rst_n_sync,
                 dut.u_core0.u_mem.rst_n_sync);
        $finish;
    end
endmodule
"""
    out = _simulate([result.rtl, MODEL], tb, tmp_path / "sim")
    assert "held=00" in out and "released=11" in out, out


def test_the_report_names_each_shell_and_its_ports(inserted: InsertResult) -> None:
    by_name = {m.memory: m for m in inserted.memories}
    assert by_name["core0_ram"].instance == "u_core0.u_mem"
    assert by_name["bank0_ram"].instance == "g_bank[0].u_mem"
    assert by_name["top_ram"].top_ports == {
        "test_mode": "top_ram_test_mode",
        "bist_start": "top_ram_bist_start",
        "bist_done": "top_ram_bist_done",
        "bist_fail": "top_ram_bist_fail",
    }
    # core is instantiated twice and only u_core0's memory is configured.
    assert inserted.copies == {"core__mbist_u_core0": "core"}
