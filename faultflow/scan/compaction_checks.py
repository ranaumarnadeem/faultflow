"""Structural verification for the static XOR-tree space compactor:
re-derives the compactor's XOR structure from the SYNTHESIZED netlist's own
connectivity and diffs it against what the manifest declares -- the same
"structural re-derivation, not SAT-based LEC" discipline as
``check_scan_structure``.

The compactor has NO register/feedback structure at all -- it's purely
combinational, a single static XOR tree -- so, unlike the decompressor's
structural check (``decompressor_check``, which simulates the ring symbolically),
this check covers the compactor completely in one pass.

Confirmed by direct inspection of real Yosys/abc output (do not "fix" this file to
trust any of these again without re-verifying against a real synthesized netlist):

1. Cell INSTANCE NAMES are Yosys/abc-generated and unstable
   (``$auto$ff.cc:337:slice$93``, ``$abc$130$auto$blifparse.cc:...``) -- never
   usable as an identification anchor. NET NAMES the RTL explicitly declares do
   survive synthesis as ``netnames`` entries, which is why this check anchors on
   those, not cell instances.
2. **Every real Sky130 library cell's ``port_directions`` dict in this project's
   synthesized JSON is EMPTY** (``{}``). Input vs. output pin roles therefore
   cannot be read from it for any real library cell: they come from the
   design's cell map (its XOR / XNOR / INV / BUF entries), exactly as the C++
   core never trusts Yosys JSON for cell semantics (CLAUDE.md: "Truth comes from the
   ``gate_type`` + port mapping + ``gate_eval.cpp`` semantics code").
3. A single-chain fanout row gets optimized to a pure net alias (no gate at
   all) -- the walker's leaf check must run BEFORE looking for a driving cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.cell_map import load_cell_map, lookup_cell
from faultflow.scan.errors import ScanError
from faultflow.scan.ring_generator import bitmask_to_index_list
from faultflow.scan.stitch import _load_json, _top_module

# The linear gate types, and whether each inverts.
_LINEAR_GATE_TYPES = {
    "XOR2": False,
    "XOR3": False,
    "BUF": False,
    "XNOR2": True,
    "XNOR3": True,
    "INV": True,
}
_DEFAULT_CELL_MAP = (
    Path(__file__).resolve().parents[2] / "cells/sky130/sky130_fd_sc_hd.json"
)

# A linear cell's (output pin, input pins, inverting).
_LinearSpec = tuple[str, tuple[str, ...], bool]


@dataclass(frozen=True)
class CompactionStructuralCheckResult:
    warnings: list[str]
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


class _LinearCells:
    """The library's linear cells, by the cell map: a cell's gate type, input pins
    (in order) and output pin come from its entry, as the C++ core takes them."""

    def __init__(self, entries: list[tuple[str, dict[str, Any]]]) -> None:
        self._entries = entries
        self._specs: dict[str, _LinearSpec | None] = {}

    def __call__(self, ctype: str) -> _LinearSpec | None:
        if ctype not in self._specs:
            self._specs[ctype] = self._spec(ctype)
        return self._specs[ctype]

    def _spec(self, ctype: str) -> _LinearSpec | None:
        entry = lookup_cell(ctype, self._entries)
        if entry is None or entry.get("node_type") != "GATE":
            return None
        inverting = _LINEAR_GATE_TYPES.get(str(entry.get("gate_type")))
        outputs = entry.get("outputs")
        if inverting is None or not isinstance(outputs, dict) or len(outputs) != 1:
            return None
        out_pin = str(next(iter(outputs.values())))
        return (out_pin, tuple(str(p) for p in entry.get("inputs", [])), inverting)


def _build_net_index(module: dict[str, Any]) -> dict[int, list[tuple[str, str, dict]]]:
    index: dict[int, list[tuple[str, str, dict]]] = {}
    for name, cell in module.get("cells", {}).items():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        ctype = str(cell.get("type", ""))
        nets_seen: set[int] = set()
        for nets in conns.values():
            for net in nets:
                if isinstance(net, int):
                    nets_seen.add(net)
        for net in nets_seen:
            index.setdefault(net, []).append((str(name), ctype, conns))
    return index


def _linear_cone(
    net_id: int,
    net_index: dict[int, list[tuple[str, str, dict]]],
    leaf_index: dict[int, int],
    cache: dict[int, tuple[int, bool]],
    linear: _LinearCells,
) -> tuple[int, bool]:
    """(coefficient bitmask over leaf_index's leaves, inverted) for net_id's
    driving cone. Raises ScanError on a non-linear gate, a fan-in count that
    doesn't match the gate's arity, or an undriven net that isn't a declared
    leaf."""
    if net_id in leaf_index:
        return (1 << leaf_index[net_id], False)
    if net_id in cache:
        return cache[net_id]
    for name, ctype, conns in net_index.get(net_id, []):
        spec = linear(ctype)
        if spec is None:
            continue
        out_pin, in_pins, inverting = spec
        if net_id not in conns.get(out_pin, []):
            continue
        input_nets = [
            n for pin in in_pins for n in conns.get(pin, []) if isinstance(n, int)
        ]
        if len(input_nets) != len(in_pins):
            raise ScanError(
                f"{name} ({ctype}): expected {len(in_pins)} input(s), "
                f"found {len(input_nets)}"
            )
        mask = 0
        combined_inv = False
        for input_net in input_nets:
            in_mask, in_inv = _linear_cone(
                input_net, net_index, leaf_index, cache, linear
            )
            mask ^= in_mask
            combined_inv = combined_inv != in_inv
        inverted = combined_inv != inverting
        cache[net_id] = (mask, inverted)
        return (mask, inverted)
    for name, ctype, _conns in net_index.get(net_id, []):
        if linear(ctype) is None:
            raise ScanError(
                f"non-linear gate {ctype!r} (instance {name}) in "
                f"compactor cone feeding net {net_id}"
            )
    raise ScanError(f"net {net_id} has no driver and is not a declared leaf")


def check_compaction_structure(
    manifest: dict[str, Any],
    cell_map_json: Path | None = None,
) -> CompactionStructuralCheckResult:
    """Re-derive each compactor output bit's XOR fanout from the synthesized,
    COMPOSED netlist (``manifest["compaction"]["composed_json"]`` -- never
    ``manifest["generic_json"]``, which must stay pointed at the plain,
    uncompacted netlist so ATPG keeps working, same rule as the decompressor
    side) and require it to match ``manifest["compaction"]["fanout"]`` exactly.

    A no-op (no errors, no warnings) when compaction is absent/disabled in
    the manifest -- opt-in, matching ``check_composed_decompressor``.
    The linear cells are the ones ``cell_map_json`` (the design's cell map,
    Sky130 HD's by default) maps to XOR, XNOR, INV and BUF gates.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        compaction = manifest.get("compaction")
        if not isinstance(compaction, dict) or not compaction.get("enabled"):
            return CompactionStructuralCheckResult(warnings=[], errors=[])

        composed_json = Path(str(compaction["composed_json"]))
        data = _load_json(composed_json)
        top = str(compaction.get("composed_top") or f"{manifest['top']}_compacted")
        _, module = _top_module(data, top)
        netnames = module.get("netnames", {})
        if not isinstance(netnames, dict):
            raise ScanError("module netnames must be an object")

        num_outputs = int(compaction["num_outputs"])
        scan_out_ports = [str(p) for p in compaction["scan_out_ports"]]
        channel_port = str(compaction.get("channel_port", "tdo"))
        fanout = compaction["fanout"]
        if len(fanout) != num_outputs:
            raise ScanError(
                "manifest compaction.fanout length does not match num_outputs"
            )

        leaf_index: dict[int, int] = {}
        for c, port in enumerate(scan_out_ports):
            if port not in netnames:
                raise ScanError(f"{port}: scan-out wire not found in netlist")
            bits = netnames[port].get("bits", [])
            if not isinstance(bits, list) or len(bits) != 1:
                raise ScanError(f"{port}: scan-out wire must be exactly one bit")
            leaf_index[int(bits[0])] = c

        if channel_port not in netnames:
            raise ScanError(
                f"declared channel_port {channel_port!r} not found in netlist"
            )
        channel_bits = netnames[channel_port].get("bits", [])
        if not isinstance(channel_bits, list) or len(channel_bits) != num_outputs:
            raise ScanError(
                f"{channel_port!r} has "
                f"{len(channel_bits) if isinstance(channel_bits, list) else '?'} "
                f"bits, expected num_outputs={num_outputs}"
            )

        net_index = _build_net_index(module)
        cache: dict[int, tuple[int, bool]] = {}
        linear = _LinearCells(load_cell_map(cell_map_json or _DEFAULT_CELL_MAP))

        for o, expected_row in zip(range(num_outputs), fanout):
            try:
                mask, inverted = _linear_cone(
                    int(channel_bits[o]), net_index, leaf_index, cache, linear
                )
            except ScanError as exc:
                errors.append(f"{channel_port}[{o}]: {exc}")
                continue
            if inverted:
                errors.append(
                    f"{channel_port}[{o}]: compactor cone is inverted, "
                    "expected a non-inverting XOR tree"
                )
                continue
            expected_mask = 0
            for chain in expected_row:
                expected_mask ^= 1 << int(chain)
            if mask != expected_mask:
                errors.append(
                    f"{channel_port}[{o}]: synthesized compactor fanout "
                    f"{bitmask_to_index_list(mask)} does not match "
                    f"manifest-declared fanout {sorted(int(c) for c in expected_row)}"
                )
    except (KeyError, ScanError, OSError) as exc:
        errors.append(str(exc))
    return CompactionStructuralCheckResult(warnings=warnings, errors=errors)
