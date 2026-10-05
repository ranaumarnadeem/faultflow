"""The IEEE 1500 wrappers of a composed SoC, as one record: every block's boundary
cells under their names and nets in the SoC, and how the SoC's own inputs set each
block's mode.

Each block's wrapper record (faultflow.wrap.record) names the block's own cells and
nets; composing the SoC (faultflow.project.assemble) renames the cells
``<instance>__<cell>`` and remaps the nets. The glue must bring each block's two mode
pins to SoC inputs through buffers and inverters alone, so a mode is a set of held
SoC inputs: each block's pins at the mode's values, an inverter on the way flipping
its input's. Two blocks sharing an input must want it at one value.

The record keeps what the wrapper modes need, as a block's does: the cells (each
with its block), and per net on a mode pin's path the value INTEST holds it at
(``mode_nets``), which decides which mode owns a stuck-at on it
(faultflow.wrap.sides); and per mode the SoC inputs it holds (``holds``).
"""

from __future__ import annotations

from typing import Any, Mapping

from faultflow.control_trace import Netlist
from faultflow.project.manifest import ProjectError
from faultflow.wrap.record import MODES

SOC_VERSION = "faultflow_soc_wrapper_v1"


def _net(remap: Mapping[int, int | str], value: Any) -> Any:
    return remap.get(value, value) if isinstance(value, int) else value


def soc_wrapper(
    module: Mapping[str, Any],
    cell_map: Mapping[str, Any],
    blocks: Mapping[str, tuple[str, Mapping[str, Any]]],
    remaps: Mapping[str, Mapping[int, int | str]],
    chains: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """The SoC's wrapper record. `blocks`: instance -> (block name, the block's
    wrapper record); `remaps`: instance -> block net -> SoC net (compose_soc);
    `chains`: the SoC scan manifest's, with their cell records."""
    netlist = Netlist(module, cell_map)
    place = {
        str(r["instance"]): (int(r["chain_index"]), int(r["chain_position"]))
        for chain in chains
        for r in chain.get("cell_records", [])
    }
    cells: list[dict[str, Any]] = []
    mode_nets: dict[int, int] = {}
    holds: dict[str, dict[str, int]] = {mode: {} for mode in MODES}
    records: list[dict[str, Any]] = []
    for instance, (name, record) in blocks.items():
        remap = remaps[instance]
        for cell in record["cells"]:
            ff = f"{instance}__{cell['ff']}"
            if ff not in place:
                raise ProjectError(f"block {name}'s wrapper flop {ff} is on no chain")
            chain, position = place[ff]
            cells.append(
                {
                    **cell,
                    "block": name,
                    "label": f"{name}/{cell['label']}",
                    "mux": f"{instance}__{cell['mux']}",
                    "gate": f"{instance}__{cell['gate']}",
                    "ff": ff,
                    "sys_net": _net(remap, cell["sys_net"]),
                    "core_net": _net(remap, cell["core_net"]),
                    "chain": chain,
                    "position": position,
                }
            )
        pins: dict[str, dict[str, Any]] = {}
        first = record["cells"][0]
        for role, intest_value in (("intest", 1), ("extest", 0)):
            net = _net(remap, record[role]["net"])
            # A cell reading the pin: an input cell's mux selects on INTEST and
            # its gate makes it safe on EXTEST; an output cell's, the reverse.
            selects = (first["side"] == "input") == (role == "intest")
            reader, pin = (first["mux"], "S") if selects else (first["gate"], "A_N")
            trace = (
                netlist.trace(net, f"{instance}__{reader}", pin)
                if isinstance(net, int)
                else None
            )
            if trace is None or trace.port is None:
                raise ProjectError(
                    f"block {name}'s {role} pin isn't driven from a SoC input "
                    "through buffers and inverters"
                )
            # The value at the block's pin in each mode, and at the SoC input.
            for mode, (intest, extest) in MODES.items():
                wanted = intest if role == "intest" else extest
                at_port = trace.value_at_pin(wanted)
                held = holds[mode].setdefault(trace.port, at_port)
                if held != at_port:
                    raise ProjectError(
                        f"SoC input {trace.port} sets the mode pins of blocks that "
                        f"need it at 0 and at 1 in {mode}"
                    )
            for step_net, _consumer, _pin, inverted in trace.steps:
                mode_nets[step_net] = intest_value ^ int(inverted)
            pins[role] = {"port": trace.port, "net": net}
        records.append({"block": name, "instance": instance, **pins})
    return {
        "version": SOC_VERSION,
        "control": "pins",
        "blocks": records,
        "mode_nets": {str(net): value for net, value in sorted(mode_nets.items())},
        "holds": holds,
        "cells": cells,
    }
