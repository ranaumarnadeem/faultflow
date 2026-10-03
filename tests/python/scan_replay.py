"""Replay exported scan patterns on the scanned sky130 netlist with the PDK's own cell
models (iverilog), cycle by cycle as scan_pattern_sim.cpp applies them: the preamble's
pulses, a load (scan enable 1, a scan-in bit per pulse), the capture pulse (scan enable
0), then an unload sampled at the low clock level before each pulse; every input the
pattern doesn't set at 0, and the pattern's capture values on the others throughout.

FaultFlow's own simulators are the reference everywhere else; this asks whether a real
chip -- whose scan flops' clear and preset act during shift too -- unloads what the
pattern expects. Scan clocks are assumed to idle at 0."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"


def _escaped(name: str) -> str:
    return f"\\{name} "


def replay_on_cells(cfg: Any, patterns_path: Path, work: Path) -> list[str]:
    """Each unload bit (not masked) that differs on the cells from the pattern's
    expected_unload, as "pattern P chain C: expected ..., cells ..."; empty when every
    one matches."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    netlist = Path(str(manifest["sky130_verilog"])).resolve()
    module = json.loads(
        Path(str(manifest["generic_json"])).read_text(encoding="utf-8")
    )["modules"][str(manifest["top"])]
    ports = module["ports"]
    inputs = [n for n, p in ports.items() if p["direction"] == "input"]
    outputs = [n for n, p in ports.items() if p["direction"] == "output"]
    clock_nets = set(manifest["clock_nets"])
    clocks = [n for n in inputs if set(ports[n]["bits"]) & clock_nets]
    se = str(manifest["scan_enable"])
    scan_ins = [str(p) for p in manifest["scan_inputs"]]
    scan_outs = [str(p) for p in manifest["scan_outputs"]]
    length = int(manifest["max_chain_length"])
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))

    lines = ["`timescale 1ns/1ps", "module tb;"]
    lines += [f"  reg {_escaped(n)}= 0;" for n in inputs]
    lines += [f"  wire {_escaped(n)};" for n in outputs]
    conns = ", ".join(f".{_escaped(n)}({_escaped(n)})" for n in inputs + outputs)
    lines += [f"  {manifest['top']} dut({conns});", "  initial begin"]

    def cycle(values: dict[str, int], sample: str | None = None) -> None:
        for name, value in values.items():
            lines.append(f"    {_escaped(name)}= {value};")
        for clock in clocks:
            lines.append(f"    {_escaped(clock)}= 0;")
        lines.append("    #5;")
        if sample is not None:
            lines.append(sample)
        for clock in clocks:
            lines.append(f"    {_escaped(clock)}= 1;")
        lines.append("    #5;")

    for index, pattern in enumerate(patterns):
        base = {n: 0 for n in inputs if n not in clocks}
        base.update(
            {n: int(v) for n, v in pattern["capture_pi_values"].items() if n in base}
        )
        base.update({se: 0, **{s: 0 for s in scan_ins}})
        for _ in range(int(pattern.get("preamble_cycles", 0))):
            cycle(base)
        for offset in range(length):
            values = dict(base, **{se: 1})
            for chain, scan_in in enumerate(scan_ins):
                bits = pattern["load_seqs"].get(str(chain), [])
                values[scan_in] = int(bits[offset]) if offset < len(bits) else 0
            cycle(values)
        cycle(base)
        for offset in range(length):
            shows = " ".join(
                f'$display("U {index} {chain} %b", {_escaped(so)});'
                for chain, so in enumerate(scan_outs)
            )
            cycle(dict(base, **{se: 1}), sample=f"    {shows}")
    lines += ["    $finish;", "  end", "endmodule"]

    work.mkdir(parents=True, exist_ok=True)
    tb = work / "replay_tb.v"
    tb.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sim = work / "replay.vvp"
    subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-DFUNCTIONAL",
            "-o",
            str(sim),
            str(tb),
            str(netlist),
            str(MODELS),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    run = subprocess.run(
        ["vvp", "-n", str(sim)], check=True, capture_output=True, text=True
    )
    got: dict[tuple[int, int], str] = {}
    for line in run.stdout.splitlines():
        if line.startswith("U "):
            _, pattern_index, chain_index, bit = line.split()
            key = (int(pattern_index), int(chain_index))
            got[key] = got.get(key, "") + bit

    differ: list[str] = []
    for index, pattern in enumerate(patterns):
        for chain in range(len(scan_outs)):
            want = [int(b) for b in pattern["expected_unload"][str(chain)]]
            have = got.get((index, chain), "")
            mask = (pattern.get("unload_mask") or {}).get(str(chain))
            compared = [
                offset
                for offset in range(len(want))
                if mask is None or offset >= len(mask) or mask[offset]
            ]
            if any(have[o : o + 1] != str(want[o]) for o in compared):
                differ.append(
                    f"pattern {index} chain {chain}: expected "
                    f"{''.join(map(str, want))}, cells {have}"
                )
    return differ
