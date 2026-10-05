"""A wrapped block as its EXTEST sees it: its boundary cells, and the rest -- its
core -- one blackbox whose outputs are unknown.

In EXTEST the output cells drive the block's ports from their flops and the input
cells capture them, while the core is held off: its outputs reach no boundary cell
past an output cell's mux, whose select the EXTEST pin holds. So EXTEST grades the
boundary cells alone. The graybox (:func:`extest_graybox`) keeps every wrapper cell
and every port, with the same net ids, and replaces every other cell by one
blackbox instance, :data:`CORE`:
- what the core drove that the cells or the ports read are its outputs, unknown
  to the scan test (an X source, faultflow.scan.x_mask, which the held EXTEST pin
  keeps out of the cells);
- what it read is its inputs, which no test observes.
The core's faults are not in it: no EXTEST campaign can call one redundant.

Its scan manifest has the wrapper chains alone, numbered from 0; the chip numbers
them after the core's. ``chains`` maps each graybox chain to the chip's, for
the patterns a tester applies to the whole block: they shift the wrapper chains'
length (``shift_length``), and the core's chains shift along, uncompared.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping

from faultflow.config import FaultflowConfig
from faultflow.control_trace import Netlist
from faultflow.wrap.cell import ROLES
from faultflow.wrap.errors import WrapError

CORE = "__core__"
CORE_TYPE = "$faultflow_core"


def with_core(
    cfg: FaultflowConfig, cores: tuple[str, ...] = (CORE,)
) -> FaultflowConfig:
    """``cfg`` for a run on a graybox: its `cores` its only blackboxes, their
    outputs unknown. Any [blackbox] instance is a cell of a core."""
    return dataclasses.replace(
        cfg,
        blackbox_instances=cores,
        blackbox_output_values=tuple((core, "x") for core in cores),
    )


@dataclass(frozen=True)
class Graybox:
    """The graybox's netlist (Yosys JSON), its scan manifest (no generic_json yet:
    the caller writes it), graybox chain -> chip chain, and the blackboxes standing
    for cores."""

    netlist: dict[str, Any]
    manifest: dict[str, Any]
    chains: dict[int, int]
    cores: tuple[str, ...] = (CORE,)


def _nets(bits: Any) -> list[int]:
    return (
        [bit for bit in bits if isinstance(bit, int)] if isinstance(bits, list) else []
    )


def _cores(cells: Mapping[str, Any], wrapper: Mapping[str, Any]) -> dict[str, set[str]]:
    """Each blackbox standing for a core, and the cells it replaces: a block's
    every cell but its wrapper's; a SoC's (faultflow.project.soc_wrapper) every cell
    of each block -- those the composition tagged with the block's name
    (``faultflow_block``) -- but its wrapper's, ``<instance>__core``, its glue
    kept."""
    kept = {str(cell[role]) for cell in wrapper["cells"] for role in ROLES}
    if "blocks" not in wrapper:
        return {CORE: set(cells) - kept}
    block_of = {
        name: cell.get("attributes", {}).get("faultflow_block")
        for name, cell in cells.items()
    }
    if not any(block_of.values()):
        raise WrapError(
            "the SoC's cells name no block (faultflow_block): compose it with "
            "its blocks tagged"
        )
    return {
        f"{block['instance']}__core": {
            name
            for name, tag in block_of.items()
            if tag == block["block"] and name not in kept
        }
        for block in wrapper["blocks"]
    }


def extest_graybox(
    netlist: Mapping[str, Any],
    manifest: Mapping[str, Any],
    cell_map: Mapping[str, Any],
) -> Graybox:
    """The EXTEST graybox of the scanned, wrapped block `netlist` -- or composed
    SoC -- whose scan `manifest` records its wrapper(s)."""
    top = str(manifest["top"])
    wrapper = manifest.get("wrapper")
    if not isinstance(wrapper, dict):
        raise WrapError("EXTEST needs the wrapper ff.py wrap puts on; there is none")
    data = copy.deepcopy(dict(netlist))
    module = data["modules"][top]
    cells: dict[str, Any] = module.get("cells", {})
    wrapper_cells = {str(cell[role]) for cell in wrapper["cells"] for role in ROLES}
    missing = sorted(wrapper_cells - set(cells))
    if missing:
        raise WrapError(f"the wrapper's cells aren't in the netlist: {missing}")
    cores = _cores(cells, wrapper)
    taken = sorted(set(cores) & set(cells))
    if taken:
        raise WrapError(f"the netlist already has a cell named {', '.join(taken)}")
    trace = Netlist(module, cell_map)

    def split(names: set[str]) -> tuple[set[int], set[int]]:
        read: set[int] = set()
        driven: set[int] = set()
        for name in names:
            entry = trace.entry(name)
            for pin, bits in cells[name].get("connections", {}).items():
                output = trace.direction(name, pin, entry) == "output"
                (driven if output else read).update(_nets(bits))
        return read, driven

    replaced = set().union(*cores.values())
    kept = set(cells) - replaced
    kept_read, kept_driven = split(kept)
    ports = module.get("ports", {})
    port_in = {
        b for p in ports.values() if p["direction"] == "input" for b in p["bits"]
    }
    port_out = {
        b for p in ports.values() if p["direction"] == "output" for b in p["bits"]
    }
    sides = {name: split(members) for name, members in cores.items()}
    new_cells = {name: cells[name] for name in cells if name in kept}
    used = kept_read | kept_driven | port_in | port_out
    for name, (core_read, core_driven) in sides.items():
        # What another core, a kept cell or a port reads of it, and what it reads
        # from them.
        elsewhere_read = kept_read | port_out
        elsewhere_driven = kept_driven | port_in
        for other, (other_read, other_driven) in sides.items():
            if other != name:
                elsewhere_read |= other_read
                elsewhere_driven |= other_driven
        outputs = sorted(core_driven & elsewhere_read)
        inputs = sorted(core_read & elsewhere_driven)
        pins = {f"o{k}": [net] for k, net in enumerate(outputs)}
        pins.update({f"i{k}": [net] for k, net in enumerate(inputs)})
        new_cells[name] = {
            "hide_name": 0,
            "type": CORE_TYPE,
            "parameters": {},
            "attributes": {"faultflow_internal": "1"},
            "port_directions": {
                pin: "output" if pin.startswith("o") else "input" for pin in pins
            },
            "connections": pins,
        }
        used |= set(outputs) | set(inputs)
    module["cells"] = new_cells
    module["netnames"] = {
        name: entry
        for name, entry in module.get("netnames", {}).items()
        if set(_nets(entry.get("bits"))) <= used
    }
    return Graybox(data, *_wrapper_manifest(manifest), tuple(sorted(cores)))


def chip_patterns(
    patterns: list[dict[str, Any]], chains: Mapping[int, int], shift_length: int
) -> list[dict[str, Any]]:
    """Exported graybox patterns as the chip takes them: each chain's bits under
    its number on the chip, and the wrapper chains' length to shift."""
    keyed = ("load_seqs", "expected_unload", "unload_mask", "launch_scan_in")
    chip: list[dict[str, Any]] = []
    for pattern in patterns:
        if pattern.get("load_care") is not None or pattern.get("seed") is not None:
            raise WrapError("a compressed pattern can't be one of EXTEST's")
        converted = dict(pattern)
        for key in keyed:
            if isinstance(pattern.get(key), dict):
                converted[key] = {
                    str(chains[int(chain)]): value
                    for chain, value in pattern[key].items()
                }
        converted["shift_length"] = shift_length
        chip.append(converted)
    return chip


def _wrapper_manifest(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[int, int]]:
    """`manifest` with the wrapper chains alone, numbered from 0, and each one's
    number on the chip."""
    for key in ("compression", "compaction"):
        if isinstance(manifest.get(key), dict) and manifest[key].get("enabled"):
            raise WrapError(f"EXTEST of a block with scan {key} isn't supported")
    chip_chains = sorted(
        (c for c in manifest["chains"] if c.get("kind") == "wrapper"),
        key=lambda chain: int(chain["index"]),
    )
    if not chip_chains:
        raise WrapError("the scan manifest has no wrapper chain")
    renumber = {int(chain["index"]): k for k, chain in enumerate(chip_chains)}

    def record(raw: Mapping[str, Any]) -> dict[str, Any]:
        return {**raw, "chain_index": renumber[int(raw["chain_index"])]}

    chains = [
        {
            **chain,
            "index": renumber[int(chain["index"])],
            "cell_records": [record(r) for r in chain.get("cell_records", [])],
        }
        for chain in chip_chains
    ]
    cells = [
        record(raw) for raw in manifest["cells"] if int(raw["chain_index"]) in renumber
    ]
    lengths = [int(chain["length"]) for chain in chains]
    graybox = {
        **manifest,
        "chain_count": len(chains),
        "cell_count": len(cells),
        "clock_nets": sorted({int(raw["clock_net"]) for raw in cells}),
        "scan_inputs": [str(chain["scan_in"]) for chain in chains],
        "scan_outputs": [str(chain["scan_out"]) for chain in chains],
        "max_chain_length": max(lengths),
        "min_chain_length": min(lengths),
        "chains": chains,
        "cells": cells,
        "ineligible_ffs": [],
    }
    return graybox, {k: int(c["index"]) for k, c in enumerate(chip_chains)}
