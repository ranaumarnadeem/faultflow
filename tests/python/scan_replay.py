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
(replay_compressed_on_cells). With scan compaction it is the composed netlist
scan-compact wrote, and the unload is compared where a tester sees it, on the
compactor's channels (replay_compacted_on_cells) -- with both, the composed
netlist holding the decompressor, the core and the compactor.

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


def _clocks(manifest: dict[str, Any]) -> list[str]:
    """The scan clocks' port names (the core's: a wrapper passes them through)."""
    ports = json.loads(Path(str(manifest["generic_json"])).read_text(encoding="utf-8"))[
        "modules"
    ][str(manifest["top"])]["ports"]
    clock_nets = set(manifest["clock_nets"])
    return [
        n
        for n, p in ports.items()
        if p["direction"] == "input" and set(p["bits"]) & clock_nets
    ]


def _techmapped(composed: Path, manifest: dict[str, Any], work: Path) -> Path:
    """A composed netlist as Verilog of sky130 cells, its generic scan cells mapped
    as `ff.py scan` maps them."""
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
    return netlist


def _drive(
    manifest: dict[str, Any],
    bench: _Bench,
    patterns: list[dict[str, Any]],
    unload_sample: Callable[[int], str],
) -> list[list[tuple[str, bool]]]:
    """Each pattern on `bench`, its scan inputs loading the chains; at each unload
    cycle `unload_sample(pattern index)`. The outputs each pattern compares."""
    se = str(manifest["scan_enable"])
    scan_ins = [str(p) for p in manifest["scan_inputs"]]
    length = int(manifest["max_chain_length"])
    base = {n: 0 for n in bench.inputs if n not in bench.clocks}
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
        for _ in range(length):
            bench.cycle(dict(shift, **{se: 1}), sample=unload_sample(index))
    return compared_outputs


def replay_on_cells(cfg: Any, patterns_path: Path, work: Path) -> list[str]:
    """Each unload bit (not masked) and each output value that differs on the cells
    from the pattern's, as "pattern P chain C: expected ..., cells ..." or "pattern
    P output O: expected ..., cells ..."; empty when every one matches."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    netlist = Path(str(manifest["sky130_verilog"])).resolve()
    module = json.loads(
        Path(str(manifest["generic_json"])).read_text(encoding="utf-8")
    )["modules"][str(manifest["top"])]
    scan_outs = [str(p) for p in manifest["scan_outputs"]]
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))
    bench = _Bench(str(manifest["top"]), module["ports"], _clocks(manifest))
    compared_outputs = _drive(
        manifest, bench, patterns, lambda index: _unloads(index, scan_outs)
    )
    lines = bench.run(work, netlist, _stubs(module, set(cfg.blackbox_instances)))
    return _compare(lines, patterns, scan_outs, compared_outputs)


def _unloads(index: int, scan_outs: list[str]) -> str:
    """The $displays of pattern `index`'s unload bits at one unload cycle."""
    return " ".join(
        f'$display("U {index} {chain} %b", {_escaped(so)});'
        for chain, so in enumerate(scan_outs)
    )


# The core's instance in a composed netlist (insert_compression, insert_compaction).
CORE_INSTANCE = "core_inst"


class _Channels:
    """The compactor's channels on a composed chip: their $displays at an unload
    cycle, and the channel bits that differ from the XOR of the expected unload
    bits each reads (manifest["compaction"]["fanout"])."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        compaction = manifest["compaction"]
        channel = str(compaction.get("channel_port", "tdo"))
        self.fanout = [[int(c) for c in row] for row in compaction["fanout"]]
        scan_outs = [str(p) for p in manifest["scan_outputs"]]
        self.chain_of = [scan_outs.index(str(p)) for p in compaction["scan_out_ports"]]
        self.refs = [
            f"{_escaped(channel)}[{o}]" if len(self.fanout) > 1 else _escaped(channel)
            for o in range(len(self.fanout))
        ]

    def sample(self, index: int) -> str:
        return " ".join(
            f'$display("C {index} {o} %b", {ref});' for o, ref in enumerate(self.refs)
        )

    def compare(
        self,
        lines: list[str],
        patterns: list[dict[str, Any]],
        length: int,
        lengths: dict[int, int] | None = None,
    ) -> list[str]:
        """The channel bits that differ, where none of the bits they read is
        masked -- an unknown bit makes the XOR unknown -- and, given `lengths`,
        each is within its chain: a shorter chain's later bits are what the
        decompressor shifts in."""
        got: dict[tuple[int, int], str] = {}
        for line in lines:
            if line.startswith("C "):
                _, pattern_index, output_index, bit = line.split()
                key = (int(pattern_index), int(output_index))
                got[key] = got.get(key, "") + bit
        differ: list[str] = []
        for index, pattern in enumerate(patterns):
            masks = pattern.get("unload_mask") or {}
            for o, row in enumerate(self.fanout):
                chains = [self.chain_of[c] for c in row]
                have = got.get((index, o), "")
                for t in range(length):
                    if lengths is not None and any(t >= lengths[c] for c in chains):
                        continue
                    if any(not masks.get(str(c), [True] * length)[t] for c in chains):
                        continue
                    want = 0
                    for c in chains:
                        want ^= int(pattern["expected_unload"][str(c)][t])
                    if have[t : t + 1] != str(want):
                        differ.append(
                            f"pattern {index} channel {o} cycle {t}: expected "
                            f"{want}, cells {have[t : t + 1]}"
                        )
        return differ


def replay_compacted_on_cells(cfg: Any, patterns_path: Path, work: Path) -> list[str]:
    """replay_on_cells on the compacted chip: the composed netlist scan-compact
    wrote, techmapped to sky130 cells. A tester sees only the compactor's channels:
    each channel bit at each unload cycle is compared with the XOR of the expected
    unload bits it reads, when none of them is masked. With compression too,
    replay_compressed_on_cells replays the chip."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("compression"):
        raise ValueError("with compression, replay_compressed_on_cells replays it")
    compaction = manifest["compaction"]
    composed = Path(str(compaction["composed_json"])).resolve()
    top = str(compaction["composed_top"])
    module = json.loads(composed.read_text(encoding="utf-8"))["modules"][top]
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))
    netlist = _techmapped(composed, manifest, work)
    bench = _Bench(top, module["ports"], _clocks(manifest))
    channels = _Channels(manifest)
    compared_outputs = _drive(manifest, bench, patterns, channels.sample)
    blackboxes = {f"{CORE_INSTANCE}__{name}" for name in cfg.blackbox_instances}
    lines = bench.run(work, netlist, _stubs(module, blackboxes))
    length = int(manifest["max_chain_length"])
    return _compare(lines, patterns, [], compared_outputs) + channels.compare(
        lines, patterns, length
    )


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
    shifts in are the decompressor's. With compaction too, the chip is the
    composed netlist scan-compact wrote around both, and the unload is compared
    on its channels (replay_compacted_on_cells). Stuck-at and launch-on-capture
    patterns only."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    compaction = manifest.get("compaction")
    if compaction and not compaction.get("with_decompressor"):
        raise ValueError("scan-compact composed this chip without the decompressor")
    chip = compaction or compression
    composed = Path(str(chip["composed_json"])).resolve()
    composed_top = str(chip["composed_top"])
    channel = str(compression.get("channel_port", "tdi"))
    width = int(compression["num_channels"])
    module = json.loads(composed.read_text(encoding="utf-8"))["modules"][composed_top]
    se = str(manifest["scan_enable"])
    scan_outs = [str(p) for p in manifest["scan_outputs"]]
    length = int(manifest["max_chain_length"])
    lengths = {int(c["index"]): int(c["length"]) for c in manifest["chains"]}
    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))
    netlist = _techmapped(composed, manifest, work)
    bench = _Bench(composed_top, module["ports"], _clocks(manifest))
    channels = _Channels(manifest) if compaction else None
    base = {n: 0 for n in bench.inputs if n not in bench.clocks}
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
        unload = channels.sample(index) if channels else _unloads(index, scan_outs)
        for _ in range(length):
            bench.cycle(dict(shift, **{se: 1}), sample=unload)
    blackboxes = {f"{CORE_INSTANCE}__{name}" for name in cfg.blackbox_instances}
    lines = bench.run(work, netlist, _stubs(module, blackboxes))
    if channels is None:
        return _compare(lines, patterns, scan_outs, compared_outputs, lengths)
    return _compare(lines, patterns, [], compared_outputs) + channels.compare(
        lines, patterns, length, lengths
    )
