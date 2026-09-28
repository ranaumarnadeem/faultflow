"""A blackbox output's value is unknown during a scan test.

An SRAM's data output holds whatever the memory last read, and no scan test
controls or knows that. The scan ATPG view ties each blackbox output to 0
(atpg_view._model_blackboxes_opaque) only because a two-valued simulator needs
some value; the scan protocol simulator reads the undriven output as 0 too.
Industry ATPG treats such an output as X. This module does the same, by
structural masking rather than a third logic value: every observation point
-- a scan flop's captured value (PPO) or a primary output -- that a blackbox
output reaches through combinational logic is unobserved. Nothing there gets
detection credit, no SAT miter targets it, and its expected bit in an exported
pattern is don't-care.

That is sound. A point outside the cone has good and faulty values that
depend only on primary inputs and loaded scan state -- a stuck-at or
transition fault removes paths, it never adds one -- so a detection there
holds whatever the memory contains. Any value SAT picks for a net inside the
cone, tie included, reaches masked points only.

Launch-on-capture adds a frame: a flop that captured an unknown value at the
launch edge holds it through the capture frame, so the capture frame's
unknowns are the blackbox outputs plus those flops' states. Launch-on-shift
loads every flop's capture-frame state by shifting, so it needs no second
frame.

A fault seen only at masked points is not redundant: the view's
blackbox-transparent twin (atpg_view.make_blackbox_transparent) drops the
mask, and the UNSAT branch classifies such a fault blackbox_unresolved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.config import FaultflowConfig
from faultflow.scan.atpg_view import (
    BLACKBOX_INSTANCE_ATTR,
    BLACKBOX_TIE_PREFIX,
    PPI_PREFIX,
    PPO_PREFIX,
    UNOBSERVED_NETS_ATTR,
)
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.errors import ScanError
from faultflow.scan.stitch import _top_module

# Flop inputs (combinational_reach's pin names) an unknown value must not
# reach: it would corrupt the scan shift itself, which no per-bit mask repairs.
# A data, enable or async set/reset input only changes what the flop captures,
# and the view's capture cone already covers that.
_SCAN_PATH_PINS = frozenset({"clock", "scan_in", "scan_enable"})


@dataclass(frozen=True)
class XMask:
    """The observation points a campaign's blackbox outputs make unknown.

    `launch_mode` is the mode the mask was computed for: None for stuck-at,
    "loc" or "los" for transition. `nets` are the view's output bits nothing
    observes; `ppo_ports` the scan flops among them (`__ppo_<inst>`), and
    `outputs` the functional outputs, spelled as the scan context's
    functional_output_order spells them. `launch_ppo_ports`, for
    launch-on-capture only, are the scan flops that capture an unknown value
    at the launch edge: whether their Q makes a transition is unknown too."""

    launch_mode: str | None = None
    nets: frozenset[int] = frozenset()
    ppo_ports: frozenset[str] = frozenset()
    outputs: frozenset[str] = frozenset()
    launch_ppo_ports: frozenset[str] = frozenset()


def launch_mode_key(transition: bool, launch_mode: str) -> str | None:
    """The XMask.launch_mode a campaign with these settings needs."""
    return launch_mode if transition else None


def x_source_nets(module: dict[str, Any], x_instances: Collection[str]) -> list[int]:
    """Net ids of the view's tie cells standing in for the outputs of
    `x_instances`: the blackboxes whose output value is unknown."""
    wanted = set(x_instances)
    nets: set[int] = set()
    for name, cell in module.get("cells", {}).items():
        if not str(name).startswith(BLACKBOX_TIE_PREFIX) or not isinstance(cell, dict):
            continue
        if cell.get("attributes", {}).get(BLACKBOX_INSTANCE_ATTR) not in wanted:
            continue
        nets.update(b for b in cell["connections"]["Y"] if isinstance(b, int))
    return sorted(nets)


def _net_of_name(module: dict[str, Any], name: str) -> int | None:
    """The net a port or net name reads, resolved the way the C++ core's
    ParsedGraph::net_id_by_name does: a port or netname by its first bit, or
    `port[i]` as bit i of the port."""
    for table in ("ports", "netnames"):
        entry = module.get(table, {}).get(name)
        if isinstance(entry, dict):
            bits = [b for b in entry.get("bits", []) if isinstance(b, int)]
            if bits:
                return bits[0]
    match = re.fullmatch(r"(.*)\[(\d+)\]", name)
    if match:
        port = module.get("ports", {}).get(match.group(1))
        if isinstance(port, dict):
            bits = port.get("bits", [])
            index = int(match.group(2))
            if index < len(bits) and isinstance(bits[index], int):
                return int(bits[index])
    return None


def compute_x_mask(
    core: Any,
    view_path: Path,
    *,
    top: str,
    cell_map: str,
    unsupported: str,
    x_instances: Collection[str],
    launch_mode: str | None,
    functional_output_order: Collection[str],
) -> XMask:
    """The view's observation points the outputs of `x_instances` reach, for a
    campaign in `launch_mode` (launch_mode_key). The cone is walked by the C++
    core on the compiled view, the same graph SAT and the simulators use."""
    _, module = _top_module(json.loads(view_path.read_text(encoding="utf-8")), top)
    sources = x_source_nets(module, x_instances)
    if not sources:
        return XMask(launch_mode=launch_mode)

    def reached(nets: list[int]) -> set[int]:
        found = core.combinational_reach(str(view_path), cell_map, nets, unsupported)
        return {int(n) for n in found["observable"]}

    ports = module.get("ports", {})

    def flops(nets: set[int]) -> set[str]:
        return {
            name
            for name, port in ports.items()
            if name.startswith(PPO_PREFIX) and port["bits"][0] in nets
        }

    masked = reached(sources)
    launch_flops: set[str] = set()
    if launch_mode == "loc":
        # A flop that captured an unknown value at launch holds it through the
        # capture frame: its state is one more source there.
        launch_flops = flops(masked)
        held = [
            ports[PPI_PREFIX + name[len(PPO_PREFIX) :]]["bits"][0]
            for name in sorted(launch_flops)
        ]
        masked = reached(sources + held)
    outputs = {
        name for name in functional_output_order if _net_of_name(module, name) in masked
    }
    return XMask(
        launch_mode,
        frozenset(masked),
        frozenset(flops(masked)),
        frozenset(outputs),
        frozenset(launch_flops),
    )


def apply_x_mask(view: dict[str, Any], top: str, mask: XMask) -> None:
    """Record `mask` in the view's top-module attributes, where the C++ core
    reads it (kUnobservedNetsAttr). An empty mask is recorded too: it says the
    mask was computed and nothing is unknown."""
    _, module = _top_module(view, top)
    attributes = module.setdefault("attributes", {})
    attributes[UNOBSERVED_NETS_ATTR] = " ".join(str(n) for n in sorted(mask.nets))


def recorded_x_mask(view: dict[str, Any], top: str) -> frozenset[int] | None:
    """The unobserved nets `view` records, or None if it records no mask."""
    _, module = _top_module(view, top)
    raw = module.get("attributes", {}).get(UNOBSERVED_NETS_ATTR)
    if raw is None:
        return None
    return frozenset(int(token) for token in str(raw).split())


def mask_scan_view(
    core: Any,
    cfg: FaultflowConfig,
    view: dict[str, Any],
    view_path: Path,
    manifest: dict[str, Any],
    generic_json: Path,
    functional_output_order: Collection[str],
    *,
    launch_mode: str | None,
) -> XMask:
    """Mask every observation point an unknown blackbox output reaches, for a
    campaign in `launch_mode` (launch_mode_key), in `view` and in the file
    `view_path` that holds it. Refuses a design where such an output reaches
    the scan path of the scanned netlist `generic_json`."""
    x_instances = cfg.blackbox_x_instances
    if not x_instances:
        return XMask(launch_mode=launch_mode)
    cell_map = str(resolve_scan_cell_map(cfg))
    unsupported = cfg.simulation.unsupported_cells
    check_x_off_scan_path(
        core,
        generic_json,
        manifest,
        cell_map=cell_map,
        unsupported=unsupported,
        blackbox_instances=cfg.blackbox_instances,
        x_instances=x_instances,
    )
    mask = compute_x_mask(
        core,
        view_path,
        top=cfg.top,
        cell_map=cell_map,
        unsupported=unsupported,
        x_instances=x_instances,
        launch_mode=launch_mode,
        functional_output_order=functional_output_order,
    )
    apply_x_mask(view, cfg.top, mask)
    view_path.write_text(
        json.dumps(view, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return mask


def check_x_off_scan_path(
    core: Any,
    generic_json: Path,
    manifest: dict[str, Any],
    *,
    cell_map: str,
    unsupported: str,
    blackbox_instances: Collection[str],
    x_instances: Collection[str],
) -> None:
    """Raise ScanError if an unknown blackbox output reaches a scan flop's
    clock, scan-in or scan-enable in the scanned netlist. There it would
    corrupt the shift, and the per-bit mask assumes a clean one."""
    top = str(manifest["top"])
    _, module = _top_module(json.loads(generic_json.read_text(encoding="utf-8")), top)
    sources: set[int] = set()
    for inst in x_instances:
        cell = module.get("cells", {}).get(inst, {})
        directions = cell.get("port_directions", {})
        for pin, bits in cell.get("connections", {}).items():
            if directions.get(pin) == "output":
                sources.update(b for b in bits if isinstance(b, int))
    if not sources:
        return
    found = core.combinational_reach(
        str(generic_json),
        cell_map,
        sorted(sources),
        unsupported,
        list(blackbox_instances),
    )
    flop_by_q = {
        int(record["q_net"]): str(record["instance"])
        for record in manifest.get("cells", [])
        if isinstance(record, dict) and "q_net" in record
    }
    bad = sorted(
        f"{flop_by_q.get(int(q), f'net {q}')}.{pin}"
        for q, pin in found["flop_inputs"]
        if pin in _SCAN_PATH_PINS
    )
    if bad:
        raise ScanError(
            "a blackbox output with an unknown value reaches the scan path of "
            + ", ".join(bad)
            + ": it would corrupt the scan shift. If the design guarantees the "
            "output's value in test mode, declare it with [blackbox] output_value."
        )
