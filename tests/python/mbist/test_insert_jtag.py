"""mbist-insert with jtag on the fixture chip: the test ports behind a JTAG TAP
and an IJTAG network (warptap), checked in simulation (Icarus Verilog).

- The chip gains the TAP's five pins and loses the test ports.
- With the TAP held in reset, and after a power-up without TRST, the chip
  behaves exactly as before: the control TDRs clear on the chip reset.
- DR scans under a board-level EXTEST, SAMPLE/PRELOAD, BYPASS or IDCODE arm no
  BIST; the same scans under IJTAG_ACCESS do, and a PDL write over the network
  rebuilt from the manifest reaches its own collar only.
- The manifest, ICL, BSDL and SDC describe what was inserted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.integrations.autombist import load_autombist_manifest
from faultflow.integrations.autombist_jtag import rebuild_network
from faultflow.mbist.insert import InsertResult, mbist_insert
from faultflow.mbist.jtag import TAP_PORTS
from faultflow.mbist.netlist import InsertError
from faultflow.mbist.spec import DesignSources
from faultflow.mbist.yosys import elaborate
from mbist_chip import CHIP, MODEL, chip_copy
from warptap_helpers import skip_unless_warptap

JTAG = "jtag: {tck_max_mhz: 10}"
OPCODE_EXTEST, OPCODE_SAMPLE_PRELOAD, OPCODE_BYPASS = 0b0000, 0b0010, 0b1111
OPCODE_IJTAG_ACCESS = 0b1100
# Each collar's control inputs, by the memory's instance path in the inserted RTL.
COLLARS = {
    "core0_ram": "dut.u_core0.u_mem.u_collar",
    "bank0_ram": "dut.\\g_bank[0].u_mem .u_collar",
    "top_ram": "dut.u_mem_top.u_collar",
}
ORIGINAL_PORTS = (
    "clk",
    "rst_n",
    "en",
    "we",
    "addr",
    "wdata",
    "rdata_core0",
    "rdata_core1",
    "rdata_bank0",
    "rdata_bank1",
    "rdata_top_q",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in ("yosys", "iverilog", "vvp")),
        reason="needs Yosys and Icarus Verilog on PATH",
    ),
]


def _jtag_chip(tmp: Path, rtl_edit: Any = None) -> Path:
    return chip_copy(tmp, lambda text: text.replace("jtag: false", JTAG), rtl_edit)


@pytest.fixture(scope="module")
def inserted(tmp_path_factory: pytest.TempPathFactory) -> InsertResult:
    skip_unless_warptap()
    tmp = tmp_path_factory.mktemp("insert_jtag")
    return mbist_insert(_jtag_chip(tmp), "chip_top", out=tmp / "out")


def _simulate(
    sources: list[Path], tb: str, workdir: Path, defines: tuple[str, ...] = ()
) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "tb.v").write_text(tb, encoding="utf-8")
    binary = workdir / "sim.vvp"
    compiled = subprocess.run(
        ["iverilog", "-g2012", *defines, "-o", str(binary), "-s", "tb"]
        + [str(workdir / "tb.v"), *map(str, sources)],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    return subprocess.run(
        ["vvp", "-n", str(binary)],
        check=True,
        capture_output=True,
        text=True,
        cwd=workdir,
    ).stdout


# The chip reset is asserted by an edge just after time 0: a reg that starts at 0
# makes no event, and an async-clear flop with no clock edge (a TCK flop, TCK
# idle) would never see the reset that hardware applies by level.
TB_HEAD = """\
`timescale 1ns/1ps
module tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst_n = 1'b1;
    initial #1 rst_n = 1'b0;
    reg [4:0] en = 5'b0;
    reg we = 1'b0;
    reg [3:0] addr = 4'b0;
    reg [7:0] wdata = 8'b0;
    wire [7:0] rdata_core0, rdata_core1, rdata_bank0, rdata_bank1, rdata_top_q;
"""

DUT = """
    chip_top dut (
        .clk(clk), .rst_n(rst_n), .en(en), .we(we), .addr(addr), .wdata(wdata),
{tap}
        .rdata_core0(rdata_core0), .rdata_core1(rdata_core1),
        .rdata_bank0(rdata_bank0), .rdata_bank1(rdata_bank1),
        .rdata_top_q(rdata_top_q)
    );
"""

TAP = "        .tck(tck), .tms(tms), .tdi(tdi), .trst_n(trst_n), .tdo(tdo),"


def _controls() -> str:
    """A Verilog expression: every collar's test_mode and bist_start."""
    return (
        "{"
        + ", ".join(f"{c}.test_mode, {c}.bist_start" for c in COLLARS.values())
        + "}"
    )


# --- what the chip looks like -------------------------------------------------


def test_the_chip_gains_the_tap_pins_and_loses_the_test_ports(
    inserted: InsertResult, tmp_path: Path
) -> None:
    again = elaborate(
        DesignSources(
            sources=(inserted.rtl,), libs=(CHIP / "macros/input_demo_8x16_scn4m.v",)
        ),
        "chip_top",
        workdir=tmp_path,
    )
    ports = tuple(again.netlist["modules"]["chip_top"]["ports"])
    assert ports == ORIGINAL_PORTS + ("tck", "tms", "tdi", "trst_n", "tdo")
    assert set(TAP_PORTS) == {"tck", "tms", "tdi", "tdo", "trst_n"}


def test_the_manifest_describes_the_network_and_its_instruction(
    inserted: InsertResult,
) -> None:
    assert inserted.manifest is not None
    manifest = load_autombist_manifest(inserted.manifest)
    access = manifest.test_access
    assert access is not None
    assert (access.network_instruction, access.network_opcode) == ("IJTAG_ACCESS", 12)
    assert (access.chip_reset, access.chip_reset_active_low) == ("rst_n", True)
    assert access.boundary_ports == ("tck", "tms", "tdi", "tdo", "trst_n")
    assert [(i.name, i.role, i.capture_sync) for i in access.instruments[:4]] == [
        ("core0_ram_test_mode", "control", False),
        ("core0_ram_bist_start", "control", False),
        ("core0_ram_bist_done", "status", True),
        ("core0_ram_bist_fail", "status", True),
    ]
    assert len(access.instruments) == 12
    categories = {i.hierarchical_path: i.category for i in access.instances}
    assert categories["u_core0.u_mem__u_collar.u_algo_top"] == "mbist_controller"
    assert categories["u_core0.u_mem__u_collar.u_sram"] == "memory"
    assert categories["g_bank[0].u_mem__u_rst_sync"] == "mbist_shell"
    assert categories["warptap_tap_core"] == "jtag_tap"
    assert categories["warptap_sib_top_ram_bist_fail"] == "ijtag_sib"
    assert categories["warptap_sib_top_ram_bist_fail_inst_0"] == "ijtag_tdr"
    assert categories["warptap_chip_reset_sync"] == "ijtag_tdr"
    # The base instances are the DFT the frozen synthesis splices in.
    assert {i.hierarchical_path for i in manifest.instances} < set(categories)
    graph, _root = rebuild_network(access)
    assert len(graph.chain) == 12


def test_the_icl_and_bsdl_name_ijtag_access(inserted: InsertResult) -> None:
    assert inserted.icl is not None and inserted.bsdl is not None
    icl = inserted.icl.read_text(encoding="utf-8")
    assert "BSDLEntity chip_top;" in icl
    assert "IJTAG_ACCESS { ScanInterface { warptap_sib_core0_ram_test_mode; } }" in icl
    bsdl = inserted.bsdl.read_text(encoding="utf-8")
    assert re.search(r'"IJTAG_ACCESS \(1100\)"', bsdl)
    assert '"BYPASS (EXTEST, SAMPLE, PRELOAD)"' in bsdl
    assert "signal is (1.000000e+07, BOTH)" in bsdl  # tck_max_mhz: 10


def _pin_path(name: str) -> list[str]:
    """get_pins' hierarchical name: levels split at "/", brackets unescaped."""
    return [part.replace("\\[", "[").replace("\\]", "]") for part in name.split("/")]


def test_every_sdc_pin_is_a_synchronizer_input_of_the_rtl(
    inserted: InsertResult, tmp_path: Path
) -> None:
    assert inserted.sdc is not None
    names = re.findall(r"get_pins \{([^}]+)\}", inserted.sdc.read_text("utf-8"))
    # Per shell: the reset and two controls; per status bit one; the TDRs' clear.
    assert len(names) == 3 * 3 + 6 + 1
    modules = elaborate(
        DesignSources(
            sources=(inserted.rtl,), libs=(CHIP / "macros/input_demo_8x16_scn4m.v",)
        ),
        "chip_top",
        workdir=tmp_path,
    ).netlist["modules"]
    for name in names:
        *instances, pin = _pin_path(name)
        module = "chip_top"
        for instance in instances:
            cell = modules[module]["cells"][instance]
            module = cell["type"]
        assert pin in modules[module]["ports"], name
        first_stage = {
            "rst_n": "faultflow_mbist_rst_sync",
            "d": "faultflow_mbist_sync2",
            "pi": "bc1_shift_only_sync",
            "chip_rst_n": "tck_reset_sync",
        }[pin]
        assert module.startswith(first_stage), name


# --- with the TAP idle, the chip is the original --------------------------------


EQUIVALENCE = """
    integer i;
    integer seed = 20261001;
    initial begin
        repeat (3) @(negedge clk);
        rst_n = 1'b1;
@AFTER_RESET@
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
@AT_END@
        $finish;
    end
endmodule
"""


def _equivalence_body(controls: bool) -> str:
    """The equivalence run; with `controls`, every collar's control inputs are
    printed after the first chip reset and at the end."""
    after = f'        #1 $display("AFTER_RESET %b", {_controls()});' if controls else ""
    end = f'        $display("AT_END %b", {_controls()});' if controls else ""
    return EQUIVALENCE.replace("@AFTER_RESET@", after).replace("@AT_END@", end)


def _cycles(out: str) -> list[str]:
    return [line for line in out.splitlines() if re.match(r"\d+ ", line)]


def _worst_case_network(inserted: InsertResult, tap_state: int) -> str:
    """Verilog that starts the TAP in `tap_state` with everything set to commit
    a 1 into every control TDR: IJTAG_ACCESS loaded, every SIB open with its
    shift bit 1, every control TDR's shift bit 1 and its output 1 already
    (names as Yosys writes them: a register that drives a port is named after
    the port)."""
    assert inserted.manifest is not None
    access = load_autombist_manifest(inserted.manifest).test_access
    assert access is not None
    lines = [
        f"        dut.warptap_tap_core.tap_state = 4'd{tap_state};",
        f"        dut.warptap_tap_core.current_instruction = 4'd{OPCODE_IJTAG_ACCESS};",
    ]
    outputs = []
    for instrument in access.instruments:
        lines += [
            f"        dut.{instrument.sib}.po = 1'b1;",
            f"        dut.{instrument.sib}.so = 1'b1;",
        ]
        if instrument.role == "control":
            for bit in instrument.tdr_bits:
                lines += [
                    f"        dut.{bit}.so = 1'b1;",
                    f"        dut.{bit}.pin_out = 1'b1;",
                ]
                outputs.append(f"dut.{bit}.pin_out")
    # Before the chip reset (just after time 0), every control TDR drives 1.
    lines.append(f'        #0.5 $display("BEFORE_RESET %b", {{{", ".join(outputs)}}});')
    return "    initial begin\n" + "\n".join(lines) + "\n    end\n"


@pytest.mark.parametrize(
    "start",
    [
        "tap_in_reset",
        "no_trst_tck_idle",
        *(f"no_trst_from_state_{s}" for s in range(16)),
    ],
)
def test_after_a_chip_reset_the_chip_behaves_as_before(
    inserted: InsertResult, tmp_path: Path, start: str
) -> None:
    """Random reads and writes, a chip reset at the start and in the middle:
    the inserted chip's outputs equal the original's on every cycle, and every
    collar's test_mode and bist_start are 0 after the chip reset. Also when TRST
    never pulsed: the flops start unknown with TCK idle, or with TCK running
    and TMS and TDI held high, as a board holds them, from every TAP state with
    the network set to commit 1s -- IJTAG_ACCESS loaded, every SIB open, every
    control TDR already 1. (Unknown is too pessimistic for the TAP itself: its
    state would stay unknown in simulation, where hardware reaches
    Test-Logic-Reset within five TCK edges.)"""
    original = _simulate(
        [CHIP / "rtl/core.v", CHIP / "rtl/chip_top.v", MODEL],
        TB_HEAD + DUT.format(tap="") + _equivalence_body(controls=False),
        tmp_path / "original",
    )
    tap = (
        "    reg tck = 1'b0;\n    reg tms = 1'b1;\n    reg trst_n = 1'b1;\n"
        "    wire tdo;\n"
    )
    if start == "tap_in_reset":
        tap += "    reg tdi = 1'b0;\n    initial #1 trst_n = 1'b0;\n"
    elif start == "no_trst_tck_idle":
        tap += "    reg tdi = 1'b0;\n"
    else:
        state = int(start.rsplit("_", 1)[1])
        tap += "    reg tdi = 1'b1;\n    always #7 tck = ~tck;\n"
        tap += _worst_case_network(inserted, state)
    after = _simulate(
        [inserted.rtl, MODEL],
        TB_HEAD + tap + DUT.format(tap=TAP) + _equivalence_body(controls=True),
        tmp_path / "inserted",
    )
    assert len(_cycles(original)) == 600
    assert _cycles(after) == _cycles(original)
    assert len({line.split()[5] for line in _cycles(original)}) > 10
    if start.startswith("no_trst_from_state"):
        assert "BEFORE_RESET 111111" in after, after  # the setup took hold
    assert "AFTER_RESET 000000" in after, after
    assert "AT_END 000000" in after, after


# --- the network moves only under IJTAG_ACCESS ---------------------------------

# Two clocks: clk for the chip, tck for the TAP. TRST and the chip reset are
# held for two TCK cycles, then the TAP idles three in Run-Test/Idle (the
# control TDRs' clear releases two TCK edges after the chip reset), then
# jtag.mem plays, one "<tms><tdi>" pair per TCK cycle from Run-Test/Idle.
JTAG_DECLS = """
    reg tck = 1'b0;
    reg tms = 1'b1;
    reg tdi = 1'b0;
    reg trst_n = 1'b0;
    wire tdo;
"""
JTAG_TB = """
    task tck_cycle;
        begin
            #25 tck = 1'b1;
            #25 tck = 1'b0;
        end
    endtask
    reg [1:0] jtag_program [0:`JTAG_CYCLES - 1];
    reg armed = 1'b0;
    always @(posedge clk)
        if (rst_n && (CONTROLS) !== 6'b0) armed <= 1'b1;
    integer i;
    initial begin
        $readmemb("jtag.mem", jtag_program);
        repeat (2) tck_cycle;
        trst_n = 1'b1;
        rst_n = 1'b1;
        tms = 1'b0;
        repeat (3) tck_cycle;
        for (i = 0; i < `JTAG_CYCLES; i = i + 1) begin
            {tms, tdi} = jtag_program[i];
            tck_cycle;
        end
        tms = 1'b0;
        repeat (20) @(posedge clk);
        $display("RESULT armed=%b controls=%b", armed, CONTROLS);
        $finish;
    end
endmodule
"""


def _run_program(inserted: InsertResult, ops: list[Any], work: Path) -> str:
    from warptap.tap_ir_play import to_cycles  # type: ignore[import-not-found]

    program = [(int(tms), int(tdi)) for tms, tdi in to_cycles(ops)]
    work.mkdir(parents=True, exist_ok=True)
    (work / "jtag.mem").write_text(
        "".join(f"{tms}{tdi}\n" for tms, tdi in program), encoding="utf-8"
    )
    tb = (
        TB_HEAD
        + JTAG_DECLS
        + DUT.format(tap=TAP)
        + JTAG_TB.replace("CONTROLS", _controls())
    )
    out = _simulate([inserted.rtl, MODEL], tb, work, (f"-DJTAG_CYCLES={len(program)}",))
    match = re.search(r"RESULT armed=(\S) controls=(\S+)", out)
    assert match, out
    return f"{match.group(1)} {match.group(2)}"


def _dr_scan(bits: int, tdi: int) -> list[Any]:
    from warptap.tap_fsm import TapState  # type: ignore[import-not-found]
    from warptap.tap_ir import GotoState, ShiftDR  # type: ignore[import-not-found]

    return [
        GotoState(TapState.SHIFT_DR),
        ShiftDR(bits, tdi=tdi),
        GotoState(TapState.RUN_TEST_IDLE),
    ]


def _select(opcode: int) -> list[Any]:
    from warptap.tap_integrity import (  # type: ignore[import-not-found]
        select_instruction,
    )

    return list(select_instruction(opcode))


@pytest.mark.parametrize(
    ("opcode", "armed"),
    [
        (None, "0"),
        (OPCODE_EXTEST, "0"),
        (OPCODE_SAMPLE_PRELOAD, "0"),
        (OPCODE_BYPASS, "0"),
        (OPCODE_IJTAG_ACCESS, "1"),
    ],
    ids=["idcode_after_reset", "extest", "sample_preload", "bypass", "ijtag_access"],
)
def test_all_ones_dr_scans_arm_the_bist_only_under_ijtag_access(
    inserted: InsertResult, tmp_path: Path, opcode: int | None, armed: str
) -> None:
    """Two all-ones scans, longer than the whole open network: under
    IJTAG_ACCESS the first opens every SIB and the second writes 1 into every
    control TDR, so every collar sees test_mode and bist_start. Under a board
    instruction (EXTEST and SAMPLE/PRELOAD select BYPASS) nothing moves."""
    loaded = [] if opcode is None else _select(opcode)
    ones = (1 << 40) - 1
    result = _run_program(inserted, loaded + _dr_scan(40, ones) * 2, tmp_path)
    assert result.split()[0] == armed, result


def test_a_pdl_write_over_the_manifests_network_reaches_its_own_collar(
    inserted: InsertResult, tmp_path: Path
) -> None:
    """The network rebuilt from the manifest is the inserted one: writing
    top_ram_test_mode = 1 sets that collar's test_mode, and nothing else."""
    from warptap.pdl_interpreter import PDLInterpreter  # type: ignore[import-not-found]

    assert inserted.manifest is not None
    access = load_autombist_manifest(inserted.manifest).test_access
    assert access is not None
    graph, root = rebuild_network(access)
    pdl = PDLInterpreter(graph, root)
    pdl.iTarget("top_ram_test_mode")
    pdl.iWrite(1)
    pdl.iApply()
    result = _run_program(
        inserted, _select(access.network_opcode) + list(pdl.program), tmp_path
    )
    # {core0 test_mode, bist_start, bank0 ..., top_ram test_mode, bist_start}
    assert result == "1 000010", result


# --- what is refused ----------------------------------------------------------


def _with_tdi_pin(tmp: Path) -> None:
    top = tmp / "rtl/chip_top.v"
    text = top.read_text(encoding="utf-8")
    text = text.replace(
        "module chip_top (", "module chip_top (\n    input wire tdi,", 1
    )
    top.write_text(text, encoding="utf-8")


def _with_a_tap_core(tmp: Path) -> None:
    """A module of the design named like warptap's TAP, instantiated (an unused
    one is dropped by elaboration and clashes with nothing)."""
    top = tmp / "rtl/chip_top.v"
    text = top.read_text(encoding="utf-8")
    text = text.replace(
        "endmodule", "    tap_core u_own_tap (.a(we), .y());\nendmodule", 1
    )
    text += (
        "\nmodule tap_core (input wire a, output wire y);\n"
        "    assign y = a;\nendmodule\n"
    )
    top.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("edit", "match"),
    [(_with_tdi_pin, "already has port"), (_with_a_tap_core, "module names the JTAG")],
    ids=["tap_pin", "module_name"],
)
def test_a_chip_the_tap_cant_be_added_to_is_refused(
    tmp_path: Path, edit: Any, match: str
) -> None:
    skip_unless_warptap()
    with pytest.raises(InsertError, match=match):
        mbist_insert(_jtag_chip(tmp_path, edit), "chip_top", out=tmp_path / "out")
