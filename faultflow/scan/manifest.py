"""Shared helpers for reading scan-manifest fields.

The manifest is produced by the stitch step; keeping these accessors in one
place avoids the drift that several copy-pasted decode blocks had accumulated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError


def manifest_clock_net_ids(
    manifest: dict[str, Any], *, error_cls: type[Exception] = ScanError
) -> list[int]:
    """Clock-domain net ids from a scan manifest.

    Supports both the v2 schema (``clock_nets``: a list) and the v1 schema
    (``clock_net``: a single int). Raises ``error_cls`` if neither is present
    (callers pass their module's error type to preserve existing behavior).
    """
    clock_nets_raw = manifest.get("clock_nets")
    if isinstance(clock_nets_raw, list) and clock_nets_raw:
        return [int(n) for n in clock_nets_raw]
    clk = manifest.get("clock_net")
    if not isinstance(clk, int):
        raise error_cls("scan manifest must have clock_nets (list) or clock_net (int)")
    return [clk]


def manifest_clock_ports(
    manifest: dict[str, Any],
    netlist_json: Path | str | None = None,
    *,
    module: dict[str, Any] | None = None,
    error_cls: type[Exception] = ScanError,
) -> list[str]:
    """Each clock domain's input port, in ``clock_nets``' order: the ports a scan
    test pulses. Stitching records them (``clock_ports``, each traced from the
    flops' pins through buffers, clock gates and held cells,
    faultflow.scan.clock_gates.clock_path); a manifest from before that is read
    by the port carrying each clock net in the top ``module``, else in
    ``netlist_json`` (the scanned netlist by default). Raises ``error_cls`` for a
    clock net no port carries."""
    nets = manifest_clock_net_ids(manifest, error_cls=error_cls)
    recorded = manifest.get("clock_ports")
    if isinstance(recorded, list) and len(recorded) == len(nets):
        return [str(port) for port in recorded]
    if module is None:
        path = Path(str(netlist_json or manifest["generic_json"]))
        data = json.loads(path.read_text(encoding="utf-8"))
        module = data["modules"][str(manifest["top"])]
    by_net = {
        port["bits"][0]: str(name)
        for name, port in (module.get("ports") or {}).items()
        if isinstance(port, dict)
        and port.get("direction") == "input"
        and len(port.get("bits", [])) == 1
    }
    ports: list[str] = []
    for net in nets:
        if net not in by_net:
            raise error_cls(f"cannot map scan clock net {net} to an input port")
        ports.append(by_net[net])
    return ports


def clock_path_nets(manifest: dict[str, Any]) -> set[int]:
    """Every net on a scan flop's clock path (``clock_paths``): its pin's, the
    root port's and each between -- clock nets (Policy 3)."""
    return {
        int(net)
        for path in (manifest.get("clock_paths") or {}).values()
        if isinstance(path, dict)
        for net in path.get("nets", [])
    }


def clock_cell_outputs(manifest: dict[str, Any]) -> set[int]:
    """The nets on the clock paths that a cell with other inputs drives: a mux a
    hold steers, an AND a hold keeps open -- not a buffer, an inverter or a clock
    gate."""
    return {
        int(net)
        for path in (manifest.get("clock_paths") or {}).values()
        if isinstance(path, dict)
        for net in path.get("cell_outputs", [])
    }
