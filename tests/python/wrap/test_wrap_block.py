"""Wrapping a block in the IEEE 1500 wrapper (faultflow.wrap.block): which port bits
get a boundary cell, how they're wired in, and that the wrapped block in the
functional mode is the block -- on the PDK's cell models, cycle by cycle."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.wrap.block import (
    CLOCK_ATTR,
    CONTROL_ATTR,
    EXTEST_ATTR,
    INTEST_ATTR,
    WrapOptions,
    wrap_block,
)
from faultflow.wrap.cell import CELL_ATTR, INDEX_ATTR, ROLE_ATTR, SIDE_ATTR
from faultflow.wrap.classify import CLOCK, CONTROL, DATA, EXCLUDED, classify
from faultflow.wrap.errors import WrapError

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
TOP = "blk"


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    outputs = ("Q", "X", "Y")
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}",
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _block(
    cells: dict[str, Any],
    inputs: dict[str, list[int | str]],
    outputs: dict[str, list[int | str]],
) -> dict[str, Any]:
    ports = {n: {"direction": "input", "bits": b} for n, b in inputs.items()}
    ports.update({n: {"direction": "output", "bits": b} for n, b in outputs.items()})
    netnames = {
        n: {"hide_name": 0, "bits": list(b), "attributes": {}}
        for n, b in {**inputs, **outputs}.items()
    }
    module = {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": netnames,
    }
    return {"modules": {TOP: module}}


def _pipeline() -> dict[str, Any]:
    """clk 2, rst_n 3 (r0's reset), a 4, b[1:0] 5 6: r0 = a & b[0] (reset to 0),
    r1 = r0 ^ b[1]; y = r1 | a, z = a (a feedthrough), k = 1, w = {r0, r1}."""
    cells = {
        "g_a": _cell("and2_1", A=4, B=5, X=10),
        "r0": _cell("dfrtp_1", CLK=2, D=10, RESET_B=3, Q=11),
        "g_x": _cell("xor2_1", A=11, B=6, X=12),
        "r1": _cell("dfxtp_1", CLK=2, D=12, Q=13),
        "g_o": _cell("or2_1", A=13, B=4, X=14),
    }
    inputs: dict[str, list[int | str]] = {
        "clk": [2],
        "rst_n": [3],
        "a": [4],
        "b": [5, 6],
    }
    outputs: dict[str, list[int | str]] = {
        "y": [14],
        "z": [4],
        "k": ["1"],
        "w": [11, 13],
    }
    return _block(cells, inputs, outputs)


def _module(netlist: dict[str, Any]) -> dict[str, Any]:
    module: dict[str, Any] = netlist["modules"][TOP]
    return module


def test_clocks_and_resets_stay_unwrapped_and_every_data_bit_is_wrapped() -> None:
    decisions = {d.bit.label: d for d in classify(_module(_pipeline()), CELL_MAP)}
    assert {label: d.reason for label, d in decisions.items()} == {
        "clk": CLOCK,
        "rst_n": CONTROL,
        "a": DATA,
        "b[0]": DATA,
        "b[1]": DATA,
        "y": DATA,
        "z": DATA,
        "k": DATA,
        "w[0]": DATA,
        "w[1]": DATA,
    }
    assert [label for label, d in decisions.items() if d.wrapped] == [
        "a",
        "b[0]",
        "b[1]",
        "y",
        "z",
        "k",
        "w[0]",
        "w[1]",
    ]


def test_exclude_leaves_ports_and_bits_unwrapped() -> None:
    decisions = classify(_module(_pipeline()), CELL_MAP, ["b[1]", "w"])
    excluded = sorted(d.bit.label for d in decisions if d.reason == EXCLUDED)
    assert excluded == ["b[1]", "w[0]", "w[1]"]


def test_brackets_in_an_exclude_glob_match_themselves() -> None:
    decisions = classify(_module(_pipeline()), CELL_MAP, ["b[*]"])
    excluded = sorted(d.bit.label for d in decisions if d.reason == EXCLUDED)
    assert excluded == ["b[0]", "b[1]"]


def test_an_input_reaching_a_reset_through_logic_is_refused() -> None:
    chip = _pipeline()
    module = _module(chip)
    module["ports"]["soft"] = {"direction": "input", "bits": [7]}
    module["cells"]["g_r"] = _cell("and2_1", A=3, B=7, X=15)
    module["cells"]["r0"]["connections"]["RESET_B"] = [15]
    with pytest.raises(WrapError, match="rst_n reaches r0.RESET_B through logic"):
        classify(module, CELL_MAP)
    decisions = classify(module, CELL_MAP, ["soft", "rst_n"])
    assert {d.bit.label for d in decisions if not d.wrapped} == {
        "clk",
        "soft",
        "rst_n",
    }


def test_inout_ports_and_scanned_or_wrapped_netlists_are_refused() -> None:
    chip = _pipeline()
    _module(chip)["ports"]["io"] = {"direction": "inout", "bits": [8]}
    with pytest.raises(WrapError, match="io is an inout port"):
        classify(_module(chip), CELL_MAP)
    assert any(d.bit.label == "io" for d in classify(_module(chip), CELL_MAP, ["io"]))
    scanned = _pipeline()
    _module(scanned)["cells"]["r1"]["type"] = "$scanff_faultflow"
    with pytest.raises(WrapError, match="already scanned"):
        classify(_module(scanned), CELL_MAP)
    wrapped = wrap_block(_pipeline(), TOP, CELL_MAP).netlist
    with pytest.raises(WrapError, match="already wrapped"):
        wrap_block(wrapped, TOP, CELL_MAP)


def test_each_wrapped_bit_gets_a_mux_a_gate_and_a_flop_wired_in() -> None:
    result = wrap_block(_pipeline(), TOP, CELL_MAP)
    module = _module(result.netlist)
    cells, ports = module["cells"], module["ports"]
    assert result.clock == "clk"
    assert [(c.label, c.side, c.index) for c in result.cells] == [
        ("a", "input", 0),
        ("b[0]", "input", 1),
        ("b[1]", "input", 2),
        ("y", "output", 3),
        ("z", "output", 4),
        ("k", "output", 5),
        ("w[0]", "output", 6),
        ("w[1]", "output", 7),
    ]
    for cell in result.cells:
        for role, instance in cell.instances.items():
            attributes = cells[instance]["attributes"]
            assert attributes[CELL_ATTR] == cell.label
            assert attributes[SIDE_ATTR] == cell.side
            assert attributes[ROLE_ATTR] == role
            assert attributes[INDEX_ATTR] == str(cell.index)
        flop = cells[cell.instances["ff"]]["connections"]
        assert flop["CLK"] == [2]
    by_label = {c.label: c for c in result.cells}
    # Nothing but the input cell reads the port bit; the core reads the cell.
    a = by_label["a"]
    readers = [
        (name, pin)
        for name, cell in cells.items()
        for pin, bits in cell["connections"].items()
        if bits == [4] and cell["port_directions"][pin] == "input"
    ]
    assert readers == [(a.instances["mux"], "A0")]
    assert cells["g_a"]["connections"]["A"] == [a.nets.out]
    assert cells["g_o"]["connections"]["B"] == [a.nets.out]
    # z echoed a: its output cell reads a's input cell, and drives z.
    z = by_label["z"]
    assert z.nets.cfi == a.nets.out
    assert ports["z"]["bits"] == [z.nets.out]
    assert by_label["k"].nets.cfi == "1"
    assert ports["w"]["bits"] == [by_label["w[0]"].nets.out, by_label["w[1]"].nets.out]
    # The mode pins: an input cell holds on intest and is safe on extest.
    intest, extest = ports["wbr_intest"]["bits"][0], ports["wbr_extest"]["bits"][0]
    assert (a.nets.hold, a.nets.safe) == (intest, extest)
    assert (z.nets.hold, z.nets.safe) == (extest, intest)
    assert module["netnames"]["z"]["bits"] == ports["z"]["bits"]
    assert module["netnames"]["__wbr_core_z"]["bits"] == [a.nets.out]
    assert {k: module["attributes"][k] for k in (CONTROL_ATTR, CLOCK_ATTR)} == {
        CONTROL_ATTR: "pins",
        CLOCK_ATTR: "clk",
    }
    assert (module["attributes"][INTEST_ATTR], module["attributes"][EXTEST_ATTR]) == (
        "wbr_intest",
        "wbr_extest",
    )


def test_an_unknown_output_bit_reads_0_through_its_cell() -> None:
    chip = _pipeline()
    _module(chip)["ports"]["w"]["bits"] = [11, "x"]
    result = wrap_block(chip, TOP, CELL_MAP)
    assert next(c for c in result.cells if c.label == "w[1]").nets.cfi == "0"


def test_the_wrapper_clock_mode_pins_and_names_are_checked() -> None:
    chip = _pipeline()
    module = _module(chip)
    module["ports"]["clk2"] = {"direction": "input", "bits": [9]}
    module["cells"]["r1"]["connections"]["CLK"] = [9]
    with pytest.raises(WrapError, match="clocks clk, clk2"):
        wrap_block(chip, TOP, CELL_MAP)
    assert wrap_block(chip, TOP, CELL_MAP, WrapOptions(clock="clk2")).clock == "clk2"
    with pytest.raises(WrapError, match="a is a port that isn't a clock"):
        wrap_block(_pipeline(), TOP, CELL_MAP, WrapOptions(clock="a"))
    with pytest.raises(WrapError, match="mode pin a is already a port"):
        wrap_block(_pipeline(), TOP, CELL_MAP, WrapOptions(intest_pin="a"))
    logic = _block(
        {"g": _cell("and2_1", A=4, B=5, X=10)},
        {"a": [4], "b": [5]},
        {"y": [10]},
    )
    with pytest.raises(WrapError, match="no clock"):
        wrap_block(logic, TOP, CELL_MAP)
    added = wrap_block(logic, TOP, CELL_MAP, WrapOptions(clock="wrck"))
    assert _module(added.netlist)["ports"]["wrck"]["direction"] == "input"


def test_the_shells_wrap_puts_the_ieee_1500_wrapper_on_without_a_model(
    tmp_path: Path,
) -> None:
    from faultflow.shell.errors import ShellError
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    source = tmp_path / "blk.json"
    source.write_text(json.dumps(_pipeline()), encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, TOP)
    session.use_lib_cells("sky130")
    bridge = TclBridge(session)
    with pytest.raises(ShellError, match="-se, -si and -so go with -model"):
        bridge.call("wrap", "-se", "wse")
    with pytest.raises(ShellError, match="-exclude, -intest and -extest go without"):
        bridge.call("wrap", "-model", "scan", "-intest", "t_int")
    result = bridge.call("wrap", "-exclude", "w,k", "-intest", "t_int")
    assert "wrapped 5 port bits (3 inputs, 2 outputs)" in result.message
    assert session.source is not None
    wrapped = json.loads(session.source.read_text(encoding="utf-8"))
    assert {"t_int", "wbr_extest"} <= set(_module(wrapped)["ports"])


@pytest.fixture
def tools() -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool} on PATH")


def test_the_wrapped_block_in_the_functional_mode_is_the_block(
    tmp_path: Path, tools: None
) -> None:
    """Both mode pins at 0: the wrapped block and the block, side by side on the
    sky130 models, give the same outputs every cycle of a random run."""
    original = _module(_pipeline())
    wrapped = _module(wrap_block(_pipeline(), TOP, CELL_MAP).netlist)
    netlist = tmp_path / "both.json"
    modules = {"blk": original, "blk_wrapped": wrapped}
    netlist.write_text(json.dumps({"modules": modules}), encoding="utf-8")
    verilog = tmp_path / "both.v"
    subprocess.run(
        ["yosys", "-q", "-p", f"read_json {netlist}; write_verilog -noattr {verilog}"],
        check=True,
    )
    rng = random.Random(1500)
    lines = [
        "`timescale 1ns/1ps",
        "module tb;",
        "  reg clk = 0, rst_n = 0, a = 0; reg [1:0] b = 0;",
        "  wire y0, z0, k0, y1, z1, k1; wire [1:0] w0, w1;",
        "  blk o (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .y(y0), .z(z0), .k(k0),"
        " .w(w0));",
        "  blk_wrapped m (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .wbr_intest(1'b0),"
        " .wbr_extest(1'b0), .y(y1), .z(z1), .k(k1), .w(w1));",
        "  initial begin",
        "    #5 rst_n = 1;",
    ]
    for _ in range(64):
        a, b = rng.randint(0, 1), rng.randint(0, 3)
        lines.append(f"    a = {a}; b = {b}; #5;")
        lines.append(
            '    $display("%b%b%b%b %b%b%b%b", y0, z0, k0, w0, y1, z1, k1, w1);'
        )
        lines.append("    clk = 1; #5 clk = 0;")
    lines += ["    $finish;", "  end", "endmodule"]
    bench = tmp_path / "tb.v"
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
    compiled = tmp_path / "tb.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-DFUNCTIONAL", "-o", str(compiled)]
        + [str(bench), str(verilog), str(MODELS)],
        check=True,
        capture_output=True,
    )
    run = subprocess.run(
        ["vvp", "-n", str(compiled)], check=True, capture_output=True, text=True
    )
    rows = [line.split() for line in run.stdout.splitlines() if " " in line.strip()]
    rows = [row for row in rows if len(row) == 2 and set("".join(row)) <= set("01x")]
    assert len(rows) == 64
    assert all(block == wrapped_block for block, wrapped_block in rows)
    assert all("x" not in row[0] for row in rows[1:])


def test_ff_py_wrap_writes_the_wrapped_block_and_its_table(
    tmp_path: Path, tools: None, capsys: pytest.CaptureFixture[str]
) -> None:
    from faultflow.cli import main

    netlist = tmp_path / "blk.json"
    netlist.write_text(json.dumps(_pipeline()), encoding="utf-8")
    ofs = tmp_path / "blk.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"output_root = {tmp_path / 'out'}\n\n[wrap]\nexclude = w\n",
        encoding="utf-8",
    )
    common = ["--top", TOP, "-c", str(ofs)]
    assert main(["wrap", "--dry-run", *common]) == 0
    table = capsys.readouterr().out
    assert "rst_n" in table and "asynchronous clear or preset" in table
    assert "w[0]" in table and "excluded" in table
    assert main(["wrap", *common]) == 0
    message = capsys.readouterr().out
    assert "wrap complete top=blk cells=6 inputs=3 outputs=3 clock=clk" in message
    out = tmp_path / "out" / TOP
    wrapped = json.loads((out / "blk_wrapped.json").read_text(encoding="utf-8"))
    assert "wbr_intest" in _module(wrapped)["ports"]
    assert "sky130_fd_sc_hd__mux2_1" in (out / "blk_wrapped.v").read_text("utf-8")
    assert "excluded" in (out / "wrap.rpt").read_text(encoding="utf-8")
    ofs.write_text(ofs.read_text("utf-8") + "clock = nope\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["wrap", *common])
