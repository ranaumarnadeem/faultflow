"""The scan chains of a composed chip, traced on its cells: from each chip input
whose net reaches a scan cell's scan input, through each cell's Q into the next
cell's scan input, to a chip output.

A project composes its SoC from blocks scanned on their own (faultflow.project.
assemble); the glue strings their chains together, or brings each to pins of its
own. Nothing records the chip's chains but its cells, so they are traced:
- a net may pass through buffers on the way, never through anything else;
- every scan cell must sit on exactly one chain, and a chain on no two inputs;
- a chain holds the flops of IEEE 1500 wrappers (faultflow.wrap) or of cores,
  never both, so each mode shifts the chains it needs alone.
:func:`chip_manifest` builds the chip's scan manifest from the chains, as
``ff.py scan`` writes a block's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from faultflow.control_trace import Netlist, one_net, port_bits
from faultflow.project.manifest import ProjectError
from faultflow.scan.reports import hash_file
from faultflow.scan.stitch import SCAN_CELL_TYPES
from faultflow.wrap.cell import ROLE_ATTR


@dataclass(frozen=True)
class TracedChain:
    """A chain's chip pins and its cells, the one nearest its scan input first."""

    scan_in: str
    scan_out: str
    cells: tuple[str, ...]
    kind: str  # "core" | "wrapper"


def _is_scan_cell(cell: Mapping[str, Any]) -> bool:
    return str(cell.get("type", "")) in SCAN_CELL_TYPES


def trace_chains(
    module: Mapping[str, Any], cell_map: Mapping[str, Any]
) -> list[TracedChain]:
    """Every scan chain of the composed `module`, in the order of its chip inputs."""
    netlist = Netlist(module, cell_map)
    readers: dict[int, list[tuple[str, str]]] = {}
    for instance, cell in netlist.cells.items():
        entry = netlist.entry(instance)
        for pin, bits in cell.get("connections", {}).items():
            if netlist.direction(instance, pin, entry) == "output":
                continue
            for bit in bits:
                if isinstance(bit, int):
                    readers.setdefault(bit, []).append((instance, pin))

    def through_buffers(net: int) -> set[int]:
        """`net` and every net a chain of buffers drives from it."""
        seen = {net}
        frontier = [net]
        while frontier:
            current = frontier.pop()
            for instance, _pin in readers.get(current, []):
                entry = netlist.entry(instance) or {}
                if entry.get("gate_type") != "BUF":
                    continue
                outputs = [
                    one_net(bits)
                    for pin, bits in netlist.cells[instance]["connections"].items()
                    if netlist.direction(instance, pin, entry) == "output"
                ]
                for out in outputs:
                    if isinstance(out, int) and out not in seen:
                        seen.add(out)
                        frontier.append(out)
        return seen

    def next_cells(net: int) -> list[str]:
        return sorted(
            {
                instance
                for reached in through_buffers(net)
                for instance, pin in readers.get(reached, [])
                if pin == "SDI" and _is_scan_cell(netlist.cells[instance])
            }
        )

    outputs = {net: name for name, net in port_bits(module, "output")}
    chains: list[TracedChain] = []
    placed: dict[str, str] = {}
    for name, net in port_bits(module, "input"):
        heads = next_cells(net)
        if not heads:
            continue
        if len(heads) > 1:
            raise ProjectError(f"chip input {name} loads several scan cells: {heads}")
        cells: list[str] = []
        current = heads[0]
        while True:
            if current in placed:
                raise ProjectError(
                    f"scan cell {current} is on the chains of {placed[current]} "
                    f"and {name}"
                )
            placed[current] = name
            cells.append(current)
            q = one_net(netlist.cells[current]["connections"].get("Q"))
            if not isinstance(q, int):
                raise ProjectError(f"scan cell {current} has no Q net")
            following = next_cells(q)
            if len(following) > 1:
                raise ProjectError(
                    f"scan cell {current} loads several scan cells: {following}"
                )
            if following:
                current = following[0]
                continue
            ends = sorted(outputs[n] for n in through_buffers(q) if n in outputs)
            if len(ends) != 1:
                raise ProjectError(
                    f"the chain from chip input {name} ends at {current}, whose Q "
                    f"reaches {len(ends)} chip outputs, not one"
                )
            break
        kinds = {
            (
                "wrapper"
                if netlist.cells[c].get("attributes", {}).get(ROLE_ATTR) == "ff"
                else "core"
            )
            for c in cells
        }
        if len(kinds) > 1:
            raise ProjectError(
                f"the chain from chip input {name} holds both wrapper and core "
                "flops: string a wrapper's chains apart from its core's"
            )
        chains.append(TracedChain(name, ends[0], tuple(cells), kinds.pop()))
    unplaced = sorted(
        name
        for name, cell in netlist.cells.items()
        if _is_scan_cell(cell) and name not in placed
    )
    if unplaced:
        raise ProjectError(f"scan cells on no chain from a chip input: {unplaced}")
    return chains


def chip_manifest(
    module: Mapping[str, Any],
    top: str,
    cell_map: Mapping[str, Any],
    generic_json: Path,
    *,
    original_types: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The scan manifest of the composed chip `module` (written at `generic_json`),
    its chains traced (trace_chains): the shape ``ff.py scan`` writes. The one
    scan enable must reach every scan cell. `original_types`: each scan cell's type
    before scan, by its name in the chip."""
    chains = trace_chains(module, cell_map)
    if not chains:
        raise ProjectError("the chip has no scan chain")
    cells = module["cells"]
    inputs = {net: name for name, net in port_bits(module, "input")}
    enables = {
        one_net(cells[name]["connections"].get("SE"))
        for chain in chains
        for name in chain.cells
    }
    enable_ports = sorted(inputs[n] for n in enables if n in inputs)
    if len(enables) != 1 or len(enable_ports) != 1:
        raise ProjectError(
            "every scan cell's scan enable must be the one chip input; found "
            f"{sorted(map(str, enables))}"
        )
    records: list[dict[str, Any]] = []
    chain_rows: list[dict[str, Any]] = []
    for index, chain in enumerate(chains):
        rows = []
        for position, name in enumerate(chain.cells):
            conns = cells[name]["connections"]
            rows.append(
                {
                    "instance": name,
                    "original_type": (original_types or {}).get(
                        name, str(cells[name].get("type", ""))
                    ),
                    "chain_index": index,
                    "chain_position": position,
                    "clock_net": one_net(conns.get("CLK")),
                    "data_net": one_net(conns.get("D")),
                    "scan_in_net": one_net(conns.get("SDI")),
                    "scan_enable_net": one_net(conns.get("SE")),
                    "q_net": one_net(conns.get("Q")),
                }
            )
        records += rows
        chain_rows.append(
            {
                "index": index,
                "kind": chain.kind,
                "scan_in": chain.scan_in,
                "scan_out": chain.scan_out,
                "length": len(chain.cells),
                "cells": list(chain.cells),
                "cell_records": rows,
            }
        )
    lengths = [len(chain.cells) for chain in chains]
    return {
        "version": 2,
        "top": top,
        "source_json": str(generic_json),
        "generic_json": str(generic_json),
        "generic_json_hash": hash_file(generic_json),
        "techmap_verilog": "",
        "sky130_verilog": None,
        "chain_order_policy": "traced from the chip's inputs, in port order",
        "chain_count": len(chains),
        "cell_count": len(records),
        "clock_nets": sorted({int(r["clock_net"]) for r in records}),
        "scan_inputs": [chain.scan_in for chain in chains],
        "scan_outputs": [chain.scan_out for chain in chains],
        "scan_enable": enable_ports[0],
        "max_chain_length": max(lengths),
        "min_chain_length": min(lengths),
        "chains": chain_rows,
        "wrapper_chains": [],
        "cells": records,
        "ineligible_ffs": [],
        "latest_check": None,
    }
