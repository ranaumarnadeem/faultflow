"""Replay exported scan patterns on the scanned sky130 netlist with the PDK's own cell
models (iverilog), cycle by cycle as scan_pattern_sim.cpp applies them: the preamble's
pulses, a load (scan enable 1, a scan-in bit per pulse), a transition pattern's launch
(a pulse, or one more shift with its launch_scan_in bits), the capture pulse (scan
enable 0), then an unload sampled at the low clock level before each pulse; every input
the pattern doesn't set at 0, the pattern's capture values on the others, and its shift
values (shift_pi_values) on theirs outside the capture. The outputs are sampled at the
low clock level before the capture edge, where the protocol samples them, and compared
with the values the pattern gives them (in capture_pi_values). A bus bit is named
"port[i]", like the pattern names it. A blackbox is a port-only stub, its outputs
unknown on the cells.

FaultFlow's own simulators are the reference everywhere else; this asks whether a real
chip -- whose scan flops' clear and preset act during shift too -- does what the
pattern expects. Scan clocks are assumed to idle at 0."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from faultflow.jtag.verify import _stubs

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"


def _escaped(name: str) -> str:
    return f"\\{name} "


def _bits(ports: dict[str, Any], direction: str) -> dict[str, str]:
    """Each bit of the ports in `direction`, by the name a pattern gives it, to its
    Verilog reference."""
    found: dict[str, str] = {}
    for name, port in ports.items():
        if port["direction"] != direction:
            continue
        width = len(port["bits"])
        if width == 1:
            found[name] = _escaped(name)
        else:
            for index in range(width):
                found[f"{name}[{index}]"] = f"{_escaped(name)}[{index}]"
    return found


def _declare(kind: str, name: str, width: int, init: str = "") -> str:
    vector = f"[{width - 1}:0] " if width > 1 else ""
    return f"  {kind} {vector}{_escaped(name)}{init};"


def replay_on_cells(cfg: Any, patterns_path: Path, work: Path) -> list[str]:
    """Each unload bit (not masked) and each output value that differs on the cells
    from the pattern's, as "pattern P chain C: expected ..., cells ..." or "pattern
    P output O: expected ..., cells ..."; empty when every one matches."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    netlist = Path(str(manifest["sky130_verilog"])).resolve()
    module = json.loads(
        Path(str(manifest["generic_json"])).read_text(encoding="utf-8")
    )["modules"][str(manifest["top"])]
    ports = module["ports"]
    in_bits = _bits(ports, "input")
    out_bits = _bits(ports, "output")
    clock_nets = set(manifest["clock_nets"])
    clocks = [
        n
        for n, p in ports.items()
        if p["direction"] == "input" and set(p["bits"]) & clock_nets
    ]
    se = str(manifest["scan_enable"])
    scan_ins = [str(p) for p in manifest["scan_inputs"]]
    scan_outs = [str(p) for p in manifest["scan_outputs"]]
    length = int(manifest["max_chain_length"])
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))

    lines = ["`timescale 1ns/1ps", "module tb;"]
    for name, port in ports.items():
        width = len(port["bits"])
        if port["direction"] == "input":
            lines.append(_declare("reg", name, width, " = 0"))
        else:
            lines.append(_declare("wire", name, width))
    conns = ", ".join(f".{_escaped(n)}({_escaped(n)})" for n in ports)
    lines += [f"  {manifest['top']} dut({conns});", "  initial begin"]

    def cycle(values: dict[str, int], sample: str | None = None) -> None:
        for name, value in values.items():
            lines.append(f"    {in_bits[name]}= {value};")
        for clock in clocks:
            lines.append(f"    {_escaped(clock)}= 0;")
        lines.append("    #5;")
        if sample is not None:
            lines.append(sample)
        for clock in clocks:
            lines.append(f"    {_escaped(clock)}= 1;")
        lines.append("    #5;")

    compared_outputs: list[list[tuple[str, bool]]] = []
    for index, pattern in enumerate(patterns):
        capture = pattern["capture_pi_values"]
        base = {n: 0 for n in in_bits if n not in clocks}
        base.update({n: int(v) for n, v in capture.items() if n in base})
        base.update({se: 0, **{s: 0 for s in scan_ins}})
        shift = dict(base)
        shift.update(
            {
                n: int(v)
                for n, v in pattern.get("shift_pi_values", {}).items()
                if n in shift
            }
        )
        for _ in range(int(pattern.get("preamble_cycles", 0))):
            cycle(shift)
        for offset in range(length):
            values = dict(shift, **{se: 1})
            for chain, scan_in in enumerate(scan_ins):
                bits = pattern["load_seqs"].get(str(chain), [])
                values[scan_in] = int(bits[offset]) if offset < len(bits) else 0
            cycle(values)
        launch = str(pattern.get("launch", ""))
        if launch == "loc":
            cycle(base)
        elif launch == "los":
            values = dict(shift, **{se: 1})
            for chain, scan_in in enumerate(scan_ins):
                bit = pattern.get("launch_scan_in", {}).get(str(chain), False)
                values[scan_in] = int(bit)
            cycle(values)
        elif launch:
            raise ValueError(f"pattern {index}: unknown launch {launch!r}")
        outputs = sorted((n, bool(v)) for n, v in capture.items() if n in out_bits)
        compared_outputs.append(outputs)
        strobes = " ".join(
            f'$display("P {index} {k} %b", {out_bits[name]});'
            for k, (name, _) in enumerate(outputs)
        )
        cycle(base, sample=f"    {strobes}" if strobes else None)
        for offset in range(length):
            shows = " ".join(
                f'$display("U {index} {chain} %b", {_escaped(so)});'
                for chain, so in enumerate(scan_outs)
            )
            cycle(dict(shift, **{se: 1}), sample=f"    {shows}")
    lines += ["    $finish;", "  end", "endmodule"]

    work.mkdir(parents=True, exist_ok=True)
    tb = work / "replay_tb.v"
    tb.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # A blackbox gets a port-only stub: its outputs float, unknown to the cells.
    stubs = work / "replay_stubs.v"
    stubs.write_text(_stubs(module, set(cfg.blackbox_instances)), encoding="utf-8")
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
            str(stubs),
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
    strobed: dict[tuple[int, int], str] = {}
    for line in run.stdout.splitlines():
        if line.startswith("U "):
            _, pattern_index, chain_index, bit = line.split()
            key = (int(pattern_index), int(chain_index))
            got[key] = got.get(key, "") + bit
        elif line.startswith("P "):
            _, pattern_index, output_index, bit = line.split()
            strobed[(int(pattern_index), int(output_index))] = bit

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
        for k, (name, value) in enumerate(compared_outputs[index]):
            have = strobed.get((index, k), "")
            if have != str(int(value)):
                differ.append(
                    f"pattern {index} output {name}: expected {int(value)}, "
                    f"cells {have}"
                )
    return differ
