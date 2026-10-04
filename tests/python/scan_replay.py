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

With scan compression the chip is the composed netlist scan-compress wrote, its
generic scan cells techmapped as `ff.py scan` maps them: each pattern's seed is held on
the channel bus and the decompressor's own cells load the chains
(replay_compressed_on_cells).

FaultFlow's own simulators are the reference everywhere else; this asks whether a real
chip -- whose scan flops' clear and preset act during shift too -- does what the
pattern expects. Scan clocks are assumed to idle at 0."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

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


class _Bench:
    """A testbench being written for `top`: its ports, then clock cycles."""

    def __init__(self, top: str, ports: dict[str, Any], clocks: list[str]) -> None:
        self.inputs = _bits(ports, "input")
        self.outputs = _bits(ports, "output")
        self.clocks = clocks
        self.lines = ["`timescale 1ns/1ps", "module tb;"]
        for name, port in ports.items():
            width = len(port["bits"])
            vector = f"[{width - 1}:0] " if width > 1 else ""
            if port["direction"] == "input":
                self.lines.append(f"  reg {vector}{_escaped(name)}= 0;")
            else:
                self.lines.append(f"  wire {vector}{_escaped(name)};")
        conns = ", ".join(f".{_escaped(n)}({_escaped(n)})" for n in ports)
        self.lines += [f"  {top} dut({conns});", "  initial begin"]

    def cycle(self, values: dict[str, int], sample: str | None = None) -> None:
        """One clock period: inputs set with the clocks low, `sample` (Verilog)
        there, then the clocks high."""
        for name, value in values.items():
            self.lines.append(f"    {self.inputs[name]}= {value};")
        for clock in self.clocks:
            self.lines.append(f"    {_escaped(clock)}= 0;")
        self.lines.append("    #5;")
        if sample is not None:
            self.lines.append(f"    {sample}")
        for clock in self.clocks:
            self.lines.append(f"    {_escaped(clock)}= 1;")
        self.lines.append("    #5;")

    def strobes(self, capture: dict[str, Any]) -> list[tuple[str, bool]]:
        """The outputs `capture` gives a value, sorted; their $displays go in the
        capture cycle's sample (`strobe_sample`)."""
        return sorted((n, bool(v)) for n, v in capture.items() if n in self.outputs)

    def strobe_sample(self, index: int, outputs: list[tuple[str, bool]]) -> str | None:
        shows = " ".join(
            f'$display("P {index} {k} %b", {self.outputs[name]});'
            for k, (name, _) in enumerate(outputs)
        )
        return shows or None

    def run(self, work: Path, netlist: Path, stubs: str) -> list[str]:
        """Compile with the cell models and the stubs, run; the output lines."""
        self.lines += ["    $finish;", "  end", "endmodule"]
        work.mkdir(parents=True, exist_ok=True)
        tb = work / "replay_tb.v"
        tb.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        stub_file = work / "replay_stubs.v"
        stub_file.write_text(stubs, encoding="utf-8")
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
                str(stub_file),
                str(MODELS),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        run = subprocess.run(
            ["vvp", "-n", str(sim)], check=True, capture_output=True, text=True
        )
        return run.stdout.splitlines()


def _compare(
    lines: list[str],
    patterns: list[dict[str, Any]],
    scan_outs: list[str],
    outputs: list[list[tuple[str, bool]]],
    lengths: dict[int, int] | None = None,
) -> list[str]:
    """The unload bits (unmasked, within `lengths` when given) and outputs that
    differ on the cells from the patterns'."""
    got: dict[tuple[int, int], str] = {}
    strobed: dict[tuple[int, int], str] = {}
    for line in lines:
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
            limit = len(want) if lengths is None else lengths[chain]
            compared = [
                offset
                for offset in range(min(limit, len(want)))
                if mask is None or offset >= len(mask) or mask[offset]
            ]
            if any(have[o : o + 1] != str(want[o]) for o in compared):
                differ.append(
                    f"pattern {index} chain {chain}: expected "
                    f"{''.join(map(str, want))}, cells {have}"
                )
        for k, (name, value) in enumerate(outputs[index]):
            have = strobed.get((index, k), "")
            if have != str(int(value)):
                differ.append(
                    f"pattern {index} output {name}: expected {int(value)}, "
                    f"cells {have}"
                )
    return differ


def _values(
    pattern: dict[str, Any], inputs: dict[str, str], base: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """The capture values (base plus the pattern's inputs) and the shift values
    (those plus its shift_pi_values)."""
    capture = dict(base)
    capture.update(
        {n: int(v) for n, v in pattern["capture_pi_values"].items() if n in inputs}
    )
    shift = dict(capture)
    shift.update(
        {
            n: int(v)
            for n, v in pattern.get("shift_pi_values", {}).items()
            if n in inputs
        }
    )
    return capture, shift


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

    bench = _Bench(str(manifest["top"]), ports, clocks)
    base = {n: 0 for n in bench.inputs if n not in clocks}
    compared_outputs: list[list[tuple[str, bool]]] = []
    for index, pattern in enumerate(patterns):
        capture, shift = _values(pattern, bench.inputs, base)
        capture.update({se: 0, **{s: 0 for s in scan_ins}})
        shift.update({se: 0, **{s: 0 for s in scan_ins}})
        for _ in range(int(pattern.get("preamble_cycles", 0))):
            bench.cycle(shift)
        for offset in range(length):
            values = dict(shift, **{se: 1})
            for chain, scan_in in enumerate(scan_ins):
                bits = pattern["load_seqs"].get(str(chain), [])
                values[scan_in] = int(bits[offset]) if offset < len(bits) else 0
            bench.cycle(values)
        launch = str(pattern.get("launch", ""))
        if launch == "loc":
            bench.cycle(capture)
        elif launch == "los":
            values = dict(shift, **{se: 1})
            for chain, scan_in in enumerate(scan_ins):
                bit = pattern.get("launch_scan_in", {}).get(str(chain), False)
                values[scan_in] = int(bit)
            bench.cycle(values)
        elif launch:
            raise ValueError(f"pattern {index}: unknown launch {launch!r}")
        outputs = bench.strobes(pattern["capture_pi_values"])
        compared_outputs.append(outputs)
        bench.cycle(capture, sample=bench.strobe_sample(index, outputs))
        for offset in range(length):
            shows = " ".join(
                f'$display("U {index} {chain} %b", {_escaped(so)});'
                for chain, so in enumerate(scan_outs)
            )
            bench.cycle(dict(shift, **{se: 1}), sample=shows)
    lines = bench.run(work, netlist, _stubs(module, set(cfg.blackbox_instances)))
    return _compare(lines, patterns, scan_outs, compared_outputs)


# insert_compression's instance of the core in the composed netlist.
CORE_INSTANCE = "core_inst"


def replay_compressed_on_cells(
    cfg: Any,
    patterns_path: Path,
    work: Path,
    seed_of: Callable[[int, dict[str, Any]], int],
) -> list[str]:
    """replay_on_cells on the compressed chip: the composed netlist scan-compress
    wrote, techmapped to sky130 cells, each pattern's seed (`seed_of(index,
    exported pattern)`, a tester's solve) held on the channel bus through the load,
    the capture and the unload, the decompressor's cells loading the chains. An
    unload bit is compared within its chain's length: the bits a shorter chain
    shifts in are the decompressor's. Stuck-at and launch-on-capture patterns only;
    no compaction."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    if manifest.get("compaction"):
        raise ValueError("the compressed replay doesn't model a compactor")
    composed = Path(str(compression["composed_json"])).resolve()
    composed_top = str(compression["composed_top"])
    channel = str(compression.get("channel_port", "tdi"))
    width = int(compression["num_channels"])
    module = json.loads(composed.read_text(encoding="utf-8"))["modules"][composed_top]
    ports = module["ports"]
    core_ports = json.loads(
        Path(str(manifest["generic_json"])).read_text(encoding="utf-8")
    )["modules"][str(manifest["top"])]["ports"]
    clock_nets = set(manifest["clock_nets"])
    clocks = [
        n
        for n, p in core_ports.items()
        if p["direction"] == "input" and set(p["bits"]) & clock_nets
    ]
    se = str(manifest["scan_enable"])
    scan_outs = [str(p) for p in manifest["scan_outputs"]]
    length = int(manifest["max_chain_length"])
    lengths = {int(c["index"]): int(c["length"]) for c in manifest["chains"]}
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))

    work.mkdir(parents=True, exist_ok=True)
    netlist = work / "composed.v"
    script = work / "composed.ys"
    script.write_text(
        f'read_json "{composed}"\n'
        f'techmap -map "{Path(str(manifest["techmap_verilog"])).resolve()}"\n'
        f'clean\nwrite_verilog -noattr "{netlist}"\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["yosys", "-q", "-s", str(script)], check=True, capture_output=True, text=True
    )

    bench = _Bench(composed_top, ports, clocks)
    base = {n: 0 for n in bench.inputs if n not in clocks}
    compared_outputs: list[list[tuple[str, bool]]] = []
    for index, pattern in enumerate(patterns):
        seed = seed_of(index, pattern)
        seeded = {f"{channel}[{k}]": (seed >> k) & 1 for k in range(width)}
        capture, shift = _values(pattern, bench.inputs, {**base, **seeded})
        capture.update({**seeded, se: 0})
        shift.update({**seeded, se: 0})
        for _ in range(int(pattern.get("preamble_cycles", 0))):
            bench.cycle(shift)
        for _ in range(length):
            bench.cycle(dict(shift, **{se: 1}))
        launch = str(pattern.get("launch", ""))
        if launch == "loc":
            bench.cycle(capture)
        elif launch:
            raise ValueError(f"pattern {index}: the compressed replay can't {launch}")
        outputs = bench.strobes(pattern["capture_pi_values"])
        compared_outputs.append(outputs)
        bench.cycle(capture, sample=bench.strobe_sample(index, outputs))
        for _ in range(length):
            shows = " ".join(
                f'$display("U {index} {chain} %b", {_escaped(so)});'
                for chain, so in enumerate(scan_outs)
            )
            bench.cycle(dict(shift, **{se: 1}), sample=shows)
    blackboxes = {f"{CORE_INSTANCE}__{name}" for name in cfg.blackbox_instances}
    lines = bench.run(work, netlist, _stubs(module, blackboxes))
    return _compare(lines, patterns, scan_outs, compared_outputs, lengths)
