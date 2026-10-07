"""A block's INTEST scan patterns, as the SoC its block sits in takes them.

The block's chains are pieces of the SoC's (faultflow.project.chip traces them on
its cells): each block chain is one run of a SoC chain's cells, in its order
(:func:`block_segments`). A block pattern's load and expected unload go to those
positions; every other SoC position is fill, loaded 0 and not compared. Every block
of the SoC is in INTEST -- its input cells hold, its output cells drive their
safe 0 -- so what the other blocks hold, and what drives the SoC's inputs, reaches
none of this block's flops.

Positions count from a chain's scan input: a load is fed deepest bit first, so
``seq[k]`` is position ``L-1-k`` of a chain of length ``L``; an unload comes out the
same way. Patterns pad a chain shorter than the longest: a load before its bits,
an unload after (faultflow.scan.protocol.serialize_vector).

The block inputs a pattern sets become the SoC inputs that drive them, through
buffers and inverters (faultflow.control_trace); its mode pins become the SoC's
INTEST holds. Launching on shift, each block chain's head takes, at the launch
shift, the bit before it on its SoC chain: that fill bit is the block pattern's
launch bit for the chain, or the SoC chain's own launch bit when the block chain
heads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.config import ConfigError
from faultflow.control_trace import Netlist, Trace, port_bits
from faultflow.scan.pattern_export import scan_pattern_from_dict, scan_pattern_to_dict
from faultflow.scan.protocol import ScanPattern


class RetargetError(ConfigError):
    """A block pattern the SoC can't apply as it is; the message says why."""


@dataclass(frozen=True)
class Segment:
    """Block chain ``block_chain`` on the SoC: SoC chain ``soc_chain``, its head at
    position ``offset`` there, ``length`` cells."""

    block_chain: int
    soc_chain: int
    offset: int
    length: int


def block_segments(
    soc_manifest: Mapping[str, Any], instance: str, block_manifest: Mapping[str, Any]
) -> list[Segment]:
    """Where each chain of the block instantiated as `instance` sits on the SoC's
    chains: its cells, ``<instance>__<cell>`` in the SoC, are one run of a SoC
    chain's, in its order."""
    soc_cells = {
        int(chain["index"]): [str(name) for name in chain["cells"]]
        for chain in soc_manifest["chains"]
    }
    place = {
        name: (index, position)
        for index, cells in soc_cells.items()
        for position, name in enumerate(cells)
    }
    segments: list[Segment] = []
    for chain in block_manifest["chains"]:
        cells = [f"{instance}__{name}" for name in chain["cells"]]
        index = int(chain["index"])
        if not cells or cells[0] not in place:
            raise RetargetError(
                f"{instance}'s scan chain {index} is on none of the SoC's chains"
            )
        soc_chain, offset = place[cells[0]]
        if soc_cells[soc_chain][offset : offset + len(cells)] != cells:
            raise RetargetError(
                f"{instance}'s scan chain {index} isn't one piece of SoC chain "
                f"{soc_chain}, in its order"
            )
        segments.append(Segment(index, soc_chain, offset, len(cells)))
    return segments


def block_inputs(
    block_module: Mapping[str, Any],
    soc_module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    instance: str,
) -> dict[str, Trace]:
    """Each input bit of the block instantiated as `instance` that a cell of it
    reads, and where that cell's pin comes from in the SoC: a SoC input, a
    constant, or logic (faultflow.control_trace.Trace)."""
    block = Netlist(block_module, cell_map)
    readers: dict[int, tuple[str, str, int]] = {}
    for name, cell in block.cells.items():
        entry = block.entry(name)
        for pin, bits in cell.get("connections", {}).items():
            if block.direction(name, pin, entry) == "output":
                continue
            for index, bit in enumerate(bits):
                if isinstance(bit, int):
                    readers.setdefault(bit, (name, str(pin), index))
    soc = Netlist(soc_module, cell_map)
    found: dict[str, Trace] = {}
    for label, net in port_bits(block_module, "input"):
        if net not in readers:
            continue  # no cell of the block reads it
        reader, pin, index = readers[net]
        consumer = f"{instance}__{reader}"
        found[label] = soc.trace(
            soc.cells[consumer]["connections"][pin][index], consumer, pin
        )
    return found


def _soc_values(
    given: Mapping[str, bool],
    inputs: Mapping[str, Trace],
    outputs: set[str],
    mode_pins: Mapping[str, int],
    where: str,
) -> dict[str, bool]:
    """The SoC input values that give the block inputs `given`'s values."""
    values: dict[str, bool] = {}
    for name, value in sorted(given.items()):
        if name in mode_pins:
            if int(value) != mode_pins[name]:
                raise RetargetError(f"{where} doesn't hold {name} as INTEST does")
            continue
        if name in outputs:
            raise RetargetError(
                f"{where} compares block output {name}, which the SoC doesn't bring "
                "to a pin"
            )
        trace = inputs.get(name)
        if trace is None:
            continue  # no cell of the block reads it
        if trace.port is None:
            if trace.const is not None and trace.const ^ trace.inverted == value:
                continue
            raise RetargetError(
                f"{where} sets block input {name}, which the SoC "
                + (
                    f"ties to {trace.const ^ trace.inverted}"
                    if trace.const is not None
                    else "drives from logic, not from a SoC input"
                )
            )
        port_value = bool(value) ^ trace.inverted
        if values.setdefault(trace.port, port_value) != port_value:
            raise RetargetError(
                f"{where} needs SoC input {trace.port} at 0 and at 1 (block input "
                f"{name} among them)"
            )
    return values


def _positions(seq: Sequence[bool], length: int, *, padded_before: bool) -> list[bool]:
    """A chain's values by position from its shift-order sequence, padded to a
    longer chain's length before (a load) or after (an unload) its own bits."""
    real = list(seq[len(seq) - length :] if padded_before else seq[:length])
    if len(real) != length:
        raise RetargetError(f"{len(seq)} bits for a chain of {length}")
    return [bool(real[length - 1 - position]) for position in range(length)]


def _shift_order(values: list[bool]) -> list[bool]:
    return values[::-1]


def retarget_pattern(
    pattern: ScanPattern,
    number: int,
    *,
    segments: Sequence[Segment],
    block_lengths: Mapping[int, int],
    soc_lengths: Mapping[int, int],
    inputs: Mapping[str, Trace],
    outputs: set[str],
    mode_pins: Mapping[str, int],
    holds: Mapping[str, int],
) -> ScanPattern:
    """Block pattern `pattern` (number `number`) as the SoC takes it."""
    where = f"block pattern {number}"
    if pattern.load_care is not None or pattern.seed is not None:
        raise RetargetError(f"{where} is a compressed load")
    if pattern.shift_length is not None:
        raise RetargetError(f"{where} shifts only some of the block's chains")
    longest = max(soc_lengths.values())
    load = {c: [False] * length for c, length in soc_lengths.items()}
    expected = {c: [False] * length for c, length in soc_lengths.items()}
    care = {c: [False] * length for c, length in soc_lengths.items()}
    owned = {c: [False] * length for c, length in soc_lengths.items()}
    for segment in segments:
        b, c, length = segment.block_chain, segment.soc_chain, segment.length
        if block_lengths[b] != length:
            raise RetargetError(f"block chain {b} has {block_lengths[b]} cells")
        if b not in pattern.load_seqs:
            raise RetargetError(f"{where} doesn't load block chain {b}")
        loaded = _positions(pattern.load_seqs[b], length, padded_before=True)
        unloaded = (
            _positions(pattern.expected_unload[b], length, padded_before=False)
            if b in pattern.expected_unload
            else [False] * length
        )
        compared = (
            _positions(pattern.unload_mask[b], length, padded_before=False)
            if pattern.unload_mask is not None and b in pattern.unload_mask
            else [b in pattern.expected_unload] * length
        )
        for p in range(length):
            position = segment.offset + p
            load[c][position] = loaded[p]
            expected[c][position] = unloaded[p]
            care[c][position] = compared[p]
            owned[c][position] = True
    launch_scan_in: dict[int, bool] = {}
    if pattern.launch == "los":
        for segment in segments:
            bit = bool(pattern.launch_scan_in.get(segment.block_chain, False))
            c, before = segment.soc_chain, segment.offset - 1
            if before < 0:
                launch_scan_in[c] = bit
            elif not owned[c][before]:
                load[c][before] = bit
                owned[c][before] = True
            elif load[c][before] != bit:
                raise RetargetError(
                    f"{where}: launching on shift, block chain "
                    f"{segment.block_chain}'s head takes the last bit of the block "
                    "chain before it on the SoC, which its load sets the other way"
                )
    values = _soc_values(pattern.capture_pi_values, inputs, outputs, mode_pins, where)
    shift_values = _soc_values(
        pattern.shift_pi_values, inputs, outputs, mode_pins, where
    )
    for port, value in holds.items():
        for given in (values, shift_values):
            if given.setdefault(port, bool(value)) != bool(value):
                raise RetargetError(
                    f"{where} needs SoC input {port}, which INTEST holds at "
                    f"{int(value)}, at {int(not value)}"
                )
    return ScanPattern(
        load_seqs={
            c: [False] * (longest - len(bits)) + _shift_order(bits)
            for c, bits in load.items()
        },
        capture_pi_values=values,
        expected_unload={
            c: _shift_order(bits) + [False] * (longest - len(bits))
            for c, bits in expected.items()
        },
        # The padding is the zeros the unload shifts in: always known.
        unload_mask={
            c: _shift_order(bits) + [True] * (longest - len(bits))
            for c, bits in care.items()
        },
        preamble_cycles=pattern.preamble_cycles,
        shift_pi_values=shift_values if pattern.shift_pi_values else {},
        launch=pattern.launch,
        launch_scan_in=launch_scan_in,
    )


def retarget_patterns(
    patterns: Sequence[Mapping[str, Any]],
    *,
    instance: str,
    block_manifest: Mapping[str, Any],
    block_module: Mapping[str, Any],
    soc_manifest: Mapping[str, Any],
    soc_module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    holds: Mapping[str, int],
) -> list[dict[str, Any]]:
    """The exported INTEST patterns of the block instantiated as `instance`, as
    exported patterns of the SoC (faultflow.scan.pattern_export): on its chains,
    from its inputs, every block in INTEST. `holds`: the SoC inputs its INTEST
    holds, and its [scan] hold."""
    wrapper = block_manifest.get("wrapper")
    if not isinstance(wrapper, dict):
        raise RetargetError(f"{instance}'s scan manifest records no wrapper")
    mode_pins = {str(wrapper["intest"]["port"]): 1, str(wrapper["extest"]["port"]): 0}
    segments = block_segments(soc_manifest, instance, block_manifest)
    inputs = block_inputs(block_module, soc_module, cell_map, instance)
    outputs = {label for label, _ in port_bits(block_module, "output")}
    block_lengths = {
        int(c["index"]): int(c["length"]) for c in block_manifest["chains"]
    }
    soc_lengths = {int(c["index"]): int(c["length"]) for c in soc_manifest["chains"]}
    return [
        scan_pattern_to_dict(
            retarget_pattern(
                scan_pattern_from_dict(dict(raw)),
                number,
                segments=segments,
                block_lengths=block_lengths,
                soc_lengths=soc_lengths,
                inputs=inputs,
                outputs=outputs,
                mode_pins=mode_pins,
                holds=holds,
            )
        )
        for number, raw in enumerate(patterns)
    ]
