"""Replay exported scan patterns on the chip's sky130 netlist with the PDK's own cell
models (iverilog): the cycles a tester applies (faultflow.scan.tester_program), every
output a cycle compares checked at its strobe. The chip is the scanned netlist, or with
scan compression or compaction the one scan-compress or scan-compact wrote -- each
pattern's seed on the compression channels and the decompressor's own cells loading
the chains, the unload read on the compactor's channels. A blackbox is a port-only
stub, its outputs unknown on the cells.

FaultFlow's own simulators are the reference everywhere else; this asks whether a real
chip -- whose scan flops' clear and preset act during shift too -- does what the
pattern expects."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from faultflow.jtag.verify import _stubs
from faultflow.scan.stil import write_stil
from faultflow.scan.tester_program import Chip, Cycle, chip_of, cycles
from stil_expand import expand, model

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"


def _escaped(name: str) -> str:
    return f"\\{name} "


def _refs(ports: dict[str, Any]) -> dict[str, str]:
    """Each port bit, by the name a pattern gives it, to its Verilog reference."""
    refs: dict[str, str] = {}
    for name, port in ports.items():
        width = len(port["bits"])
        if width == 1:
            refs[name] = _escaped(name)
        else:
            for index in range(width):
                refs[f"{name}[{index}]"] = f"{_escaped(name)}[{index}]"
    return refs


def bench(chip: Chip, program: list[Cycle]) -> str:
    """A testbench applying `program` to `chip`: per cycle its inputs with the
    clocks low, each compared output displayed at the strobe ("E <cycle> <output>
    <value>"), then the clocks high when it pulses."""
    ports = chip.module["ports"]
    refs = _refs(ports)
    lines = ["`timescale 1ns/1ps", "module tb;"]
    for name, port in ports.items():
        width = len(port["bits"])
        vector = f"[{width - 1}:0] " if width > 1 else ""
        if port["direction"] == "input":
            lines.append(f"  reg {vector}{_escaped(name)}= 0;")
        else:
            lines.append(f"  wire {vector}{_escaped(name)};")
    conns = ", ".join(f".{_escaped(n)}({_escaped(n)})" for n in ports)
    lines += [f"  {chip.top} dut({conns});", "  initial begin"]
    applied: dict[str, int] = {}
    for number, cycle in enumerate(program):
        for name, value in cycle.inputs.items():
            if applied.get(name) != value:
                lines.append(f"    {refs[name]}= {value};")
                applied[name] = value
        for clock in chip.clocks:
            lines.append(f"    {_escaped(clock)}= 0;")
        lines.append("    #5;")
        for name, want in cycle.expect.items():
            if want is not None:
                lines.append(f'    $display("E {number} {name} %b", {refs[name]});')
        if cycle.pulse:
            for clock in chip.clocks:
                lines.append(f"    {_escaped(clock)}= 1;")
        lines.append("    #5;")
    lines += ["    $finish;", "  end", "endmodule"]
    return "\n".join(lines) + "\n"


def run(chip: Chip, testbench: str, work: Path, blackboxes: set[str]) -> list[str]:
    """Compile `testbench` with the chip's netlist, the cell models and a stub per
    blackbox; run it; its output lines."""
    if chip.netlist is None:
        raise ValueError(
            "the chip's sky130 netlist wasn't written ([scan] run_techmap)"
        )
    work.mkdir(parents=True, exist_ok=True)
    tb = work / "replay_tb.v"
    tb.write_text(testbench, encoding="utf-8")
    stub_file = work / "replay_stubs.v"
    stub_file.write_text(
        _stubs(chip.module, {chip.instance_prefix + name for name in blackboxes}),
        encoding="utf-8",
    )
    sim = work / "replay.vvp"
    subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-DFUNCTIONAL",
            "-o",
            str(sim),
            str(tb),
            str(chip.netlist.resolve()),
            str(stub_file),
            str(MODELS),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["vvp", "-n", str(sim)], check=True, capture_output=True, text=True
    )
    return result.stdout.splitlines()


def differences(program: list[Cycle], lines: list[str]) -> list[str]:
    """Each compared output whose value on the cells differs from the cycle's, as
    "pattern P <phase>, cycle C, <output>: expected V, cells W"."""
    got: dict[tuple[int, str], str] = {}
    for line in lines:
        if line.startswith("E "):
            _, shown, name, value = line.split()
            got[(int(shown), name)] = value
    differ: list[str] = []
    for number, cycle in enumerate(program):
        for name, want in cycle.expect.items():
            if want is None:
                continue
            have = got.get((number, name), "")
            if have != str(want):
                differ.append(
                    f"pattern {cycle.pattern} {cycle.phase}, cycle {number}, {name}: "
                    f"expected {want}, cells {have}"
                )
    return differ


def replay_on_cells(cfg: Any, patterns_path: Path, work: Path) -> list[str]:
    """Every exported pattern, one after another from power-up, on the chip's
    cells: each compared output that differs (differences); empty when every one
    matches. The STIL write-patterns makes of them must be these cycles too
    (stil_problems)."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    chip = chip_of(manifest)
    program = cycles(chip, json.loads(patterns_path.read_text(encoding="utf-8")))
    lines = run(chip, bench(chip, program), work, set(cfg.blackbox_instances))
    return differences(program, lines) + stil_problems(manifest, chip, program, work)


def stil_problems(
    manifest: dict[str, Any], chip: Chip, program: list[Cycle], work: Path
) -> list[str]:
    """What is wrong with the STIL of `program`: expanded by STIL's rules it must
    give exactly the cycles replayed on the cells, whichever signal states a
    reader gives a procedure (stil_expand.expand), and an independent parser
    (Semi-ATE-STIL, when importable) must take it."""
    text = write_stil(chip, program, chains=manifest.get("chains", []))
    work.mkdir(parents=True, exist_ok=True)
    path = work / "patterns.stil"
    path.write_text(text, encoding="utf-8")
    problems: list[str] = []
    wanted = model(program)
    for inherit in (False, True):
        expanded = expand(text, inherit=inherit)
        if expanded == wanted:
            continue
        first = next(
            (n for n, (a, b) in enumerate(zip(expanded, wanted)) if a != b),
            min(len(expanded), len(wanted)),
        )
        reading = "inheriting" if inherit else "own"
        problems.append(
            f"STIL ({reading} procedure states) cycle {first} of {len(expanded)} "
            f"(the model has {len(wanted)}): "
            f"{expanded[first] if first < len(expanded) else None} != "
            f"{wanted[first] if first < len(wanted) else None}"
        )
    parsed = parser_verdict(path)
    if parsed:
        problems.append(f"Semi-ATE-STIL: {parsed}")
    return problems


def parser_verdict(path: Path) -> str:
    """Semi-ATE-STIL's error for the STIL at `path`, empty when it takes it or
    isn't installed (warptap's tests use it too; it's optional here)."""
    try:
        from Semi_ATE.STIL.parsers.STILParser import (  # type: ignore[import-not-found]
            STILParser,
        )
    except ImportError:
        return ""
    parser = STILParser(str(path))
    parser.parse_syntax()
    if parser.err_msg == "":
        parser.parse_semantic()  # always returns None: the verdict is err_msg
    if parser.is_parsing_done and parser.err_msg == "":
        return ""
    return str(parser.err_msg).strip() or "not parsed"
