"""The cycles a tester applies to run exported scan patterns on the chip, cycle for
cycle as FaultFlow grades them (``scan_pattern_sim.cpp``): each pattern alone -- the
preamble's pulses, the load, a transition pattern's launch, the capture, then the
unload -- or, overlapped, each load also unloading the pattern before. A cycle
gives every input but the scan clocks its value, the outputs it compares at the
strobe (the clocks low, before their edge), and whether the clocks pulse at its
end. The real-cell replay drives the PDK's cell models with these cycles, and the
STIL writer writes them.

The chip is the netlist a tester connects to: the scanned one, or with scan
compression or compaction the one ``scan-compress`` or ``scan-compact`` composed
(with both, the compacted netlist holds the decompressor too). A compressed
pattern's seed is held on the compression channels and the decompressor's cells
load the chains; a compacted unload is compared on the compactor's channels, each
channel bit the XOR of the unload bits it reads. Scan clocks idle at 0."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.scan.errors import ScanError

# A pattern's phases, in the order its cycles come.
PHASES = ("preamble", "load", "launch", "capture", "unload")

# The scanned design's instance in a composed netlist (insert_compression,
# insert_compaction): its cells are named "core_inst__<cell>" there.
CORE_INSTANCE = "core_inst"


def port_bits(ports: Mapping[str, Any], direction: str) -> list[str]:
    """Each bit of the ports in `direction`, by the name a pattern gives it: the
    port's name, or "port[i]" for bit i of a bus."""
    found: list[str] = []
    for name, port in ports.items():
        if port.get("direction") != direction:
            continue
        width = len(port.get("bits", []))
        found += [name] if width == 1 else [f"{name}[{i}]" for i in range(width)]
    return found


def _bus(port: str, width: int) -> tuple[str, ...]:
    return (port,) if width == 1 else tuple(f"{port}[{k}]" for k in range(width))


@dataclass(frozen=True)
class Chip:
    """The netlist a tester connects to and its pins' roles. Chains are numbered
    as the scan manifest numbers them."""

    top: str
    # Its top module (Yosys JSON).
    module: Mapping[str, Any] = field(repr=False)
    # The same as sky130 Verilog, when it was written.
    netlist: Path | None
    # The scanned design's cells' name prefix in it.
    instance_prefix: str
    clocks: tuple[str, ...]
    scan_enable: str
    # The pins loading each chain; none with compression.
    scan_ins: tuple[str, ...]
    # The pins unloading each chain; none with compaction.
    scan_outs: tuple[str, ...]
    # With compression, the channel bits a seed is held on: seed bit k on the k-th.
    seed_bits: tuple[str, ...]
    # With compaction, the channel bits, and the chains each reads (XORed).
    channel_bits: tuple[str, ...]
    fanout: tuple[tuple[int, ...], ...]
    chain_lengths: Mapping[int, int]
    max_chain_length: int

    @property
    def inputs(self) -> list[str]:
        return port_bits(self.module["ports"], "input")

    @property
    def outputs(self) -> list[str]:
        return port_bits(self.module["ports"], "output")


def _enabled(entry: Any) -> dict[str, Any] | None:
    return entry if isinstance(entry, dict) and entry.get("enabled") else None


def chip_of(manifest: Mapping[str, Any]) -> Chip:
    """The chip a scan manifest describes, as a tester connects to it."""
    generic = json.loads(Path(str(manifest["generic_json"])).read_text("utf-8"))
    core = generic["modules"][str(manifest["top"])]
    wrapper = sorted(
        {
            str(cell.get("type", "")).lstrip("\\")
            for cell in core.get("cells", {}).values()
            if str(cell.get("type", "")).lstrip("\\").startswith("$wbc_")
        }
    )
    if wrapper:
        raise ScanError(
            f"the chip's IEEE 1500 wrapper cells ({', '.join(wrapper)}) have no "
            "cell-level implementation, so no tester can apply its patterns"
        )
    clock_nets = set(manifest["clock_nets"])
    clocks = tuple(
        name
        for name, port in core["ports"].items()
        if port["direction"] == "input" and set(port["bits"]) & clock_nets
    )
    compression = _enabled(manifest.get("compression"))
    compaction = _enabled(manifest.get("compaction"))
    if compression and compaction and not compaction.get("with_decompressor"):
        raise ScanError(
            "scan-compact composed the chip before scan-compress inserted the "
            "decompressor: run scan-compact again"
        )
    composed = compaction or compression
    if composed is None:
        top, module, prefix = str(manifest["top"]), core, ""
        verilog = manifest.get("sky130_verilog")
    else:
        top = str(composed["composed_top"])
        data = json.loads(Path(str(composed["composed_json"])).read_text("utf-8"))
        module, prefix = data["modules"][top], f"{CORE_INSTANCE}__"
        verilog = composed.get("sky130_verilog")
    scan_outputs = [str(p) for p in manifest["scan_outputs"]]
    fanout: tuple[tuple[int, ...], ...] = ()
    channel_bits: tuple[str, ...] = ()
    if compaction:
        read = [str(p) for p in compaction["scan_out_ports"]]
        fanout = tuple(
            tuple(scan_outputs.index(read[int(c)]) for c in row)
            for row in compaction["fanout"]
        )
        channel_bits = _bus(str(compaction.get("channel_port", "tdo")), len(fanout))
    seed_bits: tuple[str, ...] = ()
    if compression:
        port = str(compression.get("channel_port", "tdi"))
        seed_bits = _bus(port, int(compression["num_channels"]))
    return Chip(
        top=top,
        module=module,
        netlist=Path(str(verilog)) if verilog else None,
        instance_prefix=prefix,
        clocks=clocks,
        scan_enable=str(manifest["scan_enable"]),
        scan_ins=(
            () if compression else tuple(str(p) for p in manifest["scan_inputs"])
        ),
        scan_outs=() if compaction else tuple(scan_outputs),
        seed_bits=seed_bits,
        channel_bits=channel_bits,
        fanout=fanout,
        chain_lengths={int(c["index"]): int(c["length"]) for c in manifest["chains"]},
        max_chain_length=int(manifest["max_chain_length"]),
    )


@dataclass(frozen=True)
class Cycle:
    """One tester cycle of pattern `pattern`'s `phase`: every input but the scan
    clocks at its value in `inputs` and the clocks low; at the strobe, each output
    in `expect` compared with its value (None: not compared); then the clocks
    pulse when `pulse`."""

    pattern: int
    phase: str
    inputs: Mapping[str, int]
    expect: Mapping[str, int | None]
    pulse: bool = True
    # The pattern whose unload the cycle compares: the pattern itself in its
    # unload, the one before in a load that overlaps that one's unload. None: no
    # unload.
    unloading: int | None = None


def cycles(
    chip: Chip, patterns: Sequence[Mapping[str, Any]], *, overlap: bool = False
) -> list[Cycle]:
    """Every exported pattern's cycles on `chip`, one pattern after another. An
    input a pattern doesn't set is 0; its shift values (shift_pi_values) hold
    everywhere but the launch on capture and the capture.

    Without `overlap` each pattern is applied alone, as FaultFlow grades it: its
    preamble, load, launch, capture, then its unload. With it, each load also
    unloads the pattern before -- a tester's usual way, half the shifts. Nothing an
    unload compares depends on what shifts in or on the inputs the load holds.
    Every flop a preamble settles stays settled under the holds, so the longest
    preamble is given once, first, and each capture's pulse, scan enable off, arms
    the decompressor's reseed for the next load. A last unload ends the patterns."""
    inputs = [name for name in chip.inputs if name not in chip.clocks]
    outputs = set(chip.outputs)
    own = [
        _pattern_cycles(chip, inputs, outputs, index, pattern)
        for index, pattern in enumerate(patterns)
    ]
    if not overlap:
        return [cycle for pattern_cycles in own for cycle in pattern_cycles]
    program: list[Cycle] = []
    if own:
        preamble = max(sum(c.phase == "preamble" for c in o) for o in own)
        program += [_preamble_cycle(chip, own[0])] * preamble
    previous: list[Cycle] = []  # the unload of the pattern before
    for pattern_cycles in own:
        loads = [cycle for cycle in pattern_cycles if cycle.phase == "load"]
        for t, load in enumerate(loads):
            if previous:
                unload = previous[t]
                load = replace(
                    load, expect=dict(unload.expect), unloading=unload.pattern
                )
            program.append(load)
        program += [c for c in pattern_cycles if c.phase in ("launch", "capture")]
        previous = [cycle for cycle in pattern_cycles if cycle.phase == "unload"]
    return program + previous


def _preamble_cycle(chip: Chip, first: list[Cycle]) -> Cycle:
    """A preamble cycle of the first pattern: its shift values, scan enable off."""
    for cycle in first:
        if cycle.phase == "preamble":
            return cycle
    load = next(cycle for cycle in first if cycle.phase == "load")
    values = dict(load.inputs)
    values[chip.scan_enable] = 0
    values.update(dict.fromkeys(chip.scan_ins, 0))
    return Cycle(load.pattern, "preamble", values, {})


def _pattern_cycles(
    chip: Chip,
    inputs: list[str],
    outputs: set[str],
    index: int,
    pattern: Mapping[str, Any],
) -> list[Cycle]:
    capture = dict.fromkeys(inputs, 0)
    given = pattern["capture_pi_values"]
    capture.update({n: int(v) for n, v in given.items() if n in capture})
    shift = dict(capture)
    shift.update(
        {n: int(v) for n, v in pattern.get("shift_pi_values", {}).items() if n in shift}
    )
    for values in (capture, shift):
        values.update(_seeded(chip, pattern, index))
        values.update(dict.fromkeys(chip.scan_ins, 0))
        values[chip.scan_enable] = 0
    own: list[Cycle] = []
    for _ in range(int(pattern.get("preamble_cycles", 0))):
        own.append(Cycle(index, "preamble", dict(shift), {}))
    for t in range(chip.max_chain_length):
        own.append(
            Cycle(index, "load", _shifting(chip, shift, pattern["load_seqs"], t), {})
        )
    launch = str(pattern.get("launch", ""))
    if launch == "loc":
        own.append(Cycle(index, "launch", dict(capture), {}))
    elif launch == "los":
        heads = {c: [b] for c, b in pattern.get("launch_scan_in", {}).items()}
        own.append(Cycle(index, "launch", _shifting(chip, shift, heads, 0), {}))
    elif launch:
        raise ScanError(f"pattern {index}: unknown launch {launch!r}")
    strobed = {n: int(v) for n, v in given.items() if n in outputs}
    own.append(Cycle(index, "capture", dict(capture), dict(strobed)))
    for t in range(chip.max_chain_length):
        shifting = _shifting(chip, shift, {}, t)
        unload = _unloaded(chip, pattern, t)
        own.append(Cycle(index, "unload", shifting, unload, unloading=index))
    return own


def _seeded(chip: Chip, pattern: Mapping[str, Any], index: int) -> dict[str, int]:
    """With compression, the pattern's seed on the channel bits."""
    if not chip.seed_bits:
        return {}
    seed = pattern.get("seed")
    if seed is None:
        raise ScanError(
            f"pattern {index} has no seed: a compressed pattern is exported with "
            "its decompressor seed (export the patterns again)"
        )
    return {bit: (int(seed) >> k) & 1 for k, bit in enumerate(chip.seed_bits)}


def _shifting(
    chip: Chip, values: Mapping[str, int], bits: Mapping[str, Any], t: int
) -> dict[str, int]:
    """`values` with scan enable on and each scan in at its chain's bit t in
    `bits` (by chain, as exported: 0 past its end)."""
    shifted = dict(values)
    shifted[chip.scan_enable] = 1
    for chain, pin in enumerate(chip.scan_ins):
        chain_bits = bits.get(str(chain), [])
        shifted[pin] = int(chain_bits[t]) if t < len(chain_bits) else 0
    return shifted


def _unloaded(chip: Chip, pattern: Mapping[str, Any], t: int) -> dict[str, int | None]:
    """The unload's expected values at cycle t: each scan out's, or each
    compaction channel's, the XOR of the bits it reads."""
    masks = pattern.get("unload_mask") or {}

    def known(chain: int) -> int | None:
        mask = masks.get(str(chain))
        if mask is not None and t < len(mask) and not mask[t]:
            return None  # a flop that captured an unknown value
        if chip.seed_bits and t >= chip.chain_lengths.get(chain, 0):
            return None  # what the decompressor shifts through a shorter chain
        bits = pattern["expected_unload"].get(str(chain), [])
        return int(bits[t]) if t < len(bits) else None

    if not chip.channel_bits:
        return {pin: known(chain) for chain, pin in enumerate(chip.scan_outs)}
    expect: dict[str, int | None] = {}
    for pin, row in zip(chip.channel_bits, chip.fanout):
        read = [known(chain) for chain in row]
        bit = 0
        for value in read:
            bit ^= value or 0
        expect[pin] = None if None in read else bit
    return expect
