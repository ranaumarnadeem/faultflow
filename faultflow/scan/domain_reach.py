"""Structural cross-domain reachability for transition fault classification.

For transition faults in multi-clock designs, a fault at net N is testable
within a single domain Di if Di-domain FF-Q outputs can reach N (launch path)
AND N can reach Di-domain FF-D inputs or primary outputs (capture path),
both through purely combinational logic.

If no single domain satisfies both, the fault is cross-domain and is tagged
``excluded_cross_domain`` per Policy 3 (tagged, never silently skipped).

Only applies when fault_model == "transition" and there are >= 2 clock domains.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

# Scan-control pins that are NOT data paths through the FF.
_SCAN_CONTROL_PINS: frozenset[str] = frozenset({"CLK", "SE", "SCE", "SDI", "SI"})

# Our internal scan FF type (created by stitch.py).
_INTERNAL_SCAN_FF = "\\$scanff_faultflow"
_INTERNAL_SCAN_FF_ALT = "$scanff_faultflow"


def _all_int_bits(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [b for b in value if isinstance(b, int)]


def _is_output_pin(
    cell_type: str,
    pin: str,
    port_directions: dict[str, str] | None,
    cell_map: dict[str, Any] | None,
) -> bool:
    """Return True if ``pin`` is an output of the cell."""
    if port_directions is not None:
        return port_directions.get(pin) == "output"
    # Fall back to cell map lookup.
    if cell_map is not None:
        for pattern, entry in cell_map.items():
            if fnmatch.fnmatch(cell_type, pattern):
                outputs = entry.get("outputs", {})
                if isinstance(outputs, dict):
                    return pin in outputs
                break
    # Hardcoded fallback for our internal scan FF type.
    bare = cell_type.lstrip("\\")
    if bare in ("$scanff_faultflow",):
        return pin == "Q"
    # Sky130 combinational cells commonly use Y or X as output.
    return pin in ("Y", "X", "Q", "CO", "SUM", "S")


def compute_cross_domain_net_ids(
    generic_json_path: Path | str,
    manifest: dict[str, Any],
    cell_map_path: Path | str | None = None,
) -> set[int]:
    """Return the set of yosys net IDs that are cross-domain for transition faults.

    A net is cross-domain if no single clock domain can both:
    - launch a transition there (forward reachable from that domain's FF-Q outputs), AND
    - capture the fault (forward reachable to that domain's FF-D inputs or any PO).

    PI (primary input) nets are never cross-domain; they are directly controllable.
    """
    clock_nets_raw = manifest.get("clock_nets", [])
    if not isinstance(clock_nets_raw, list) or len(clock_nets_raw) <= 1:
        return set()  # Single domain — no cross-domain exclusions possible.

    clock_nets: list[int] = [int(n) for n in clock_nets_raw]

    data: dict[str, Any] = json.loads(
        Path(generic_json_path).read_text(encoding="utf-8")
    )
    top = str(manifest["top"])
    module = data["modules"][top]

    cell_map: dict[str, Any] | None = None
    if cell_map_path is not None:
        cell_map = json.loads(Path(cell_map_path).read_text(encoding="utf-8"))

    # Build FF → clock domain from manifest.
    ff_clock_net: dict[str, int] = {}
    for record in manifest.get("cells", []):
        if isinstance(record, dict):
            inst = str(record.get("instance", ""))
            clk = record.get("clock_net")
            if inst and isinstance(clk, int):
                ff_clock_net[inst] = int(clk)

    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return set()

    ff_instances: set[str] = set(ff_clock_net.keys())

    # PI and PO nets.
    pi_nets: set[int] = set()
    po_nets: set[int] = set()
    for port_data in module.get("ports", {}).values():
        if not isinstance(port_data, dict):
            continue
        bits = _all_int_bits(port_data.get("bits"))
        direction = port_data.get("direction", "")
        if direction == "input":
            pi_nets.update(bits)
        elif direction == "output":
            po_nets.update(bits)

    # Build sinks_of[net] = [(instance, pin)] and driver_of[net] = (instance, pin).
    sinks_of: dict[int, list[tuple[str, str]]] = {}
    driver_of: dict[int, tuple[str, str]] = {}

    # Q-nets (launch sources) and D-nets (capture sinks) per domain.
    domain_q_nets: dict[int, set[int]] = {c: set() for c in clock_nets}
    domain_d_nets: dict[int, set[int]] = {c: set() for c in clock_nets}

    for instance, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("type", ""))
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        port_dirs_raw = cell.get("port_directions")
        port_dirs = (
            {str(k): str(v) for k, v in port_dirs_raw.items()}
            if isinstance(port_dirs_raw, dict)
            else None
        )
        is_ff = instance in ff_instances
        clk_domain = ff_clock_net.get(instance) if is_ff else None

        for pin, bits_raw in conns.items():
            bits = _all_int_bits(bits_raw)
            is_out = _is_output_pin(cell_type, pin, port_dirs, cell_map)
            for bit in bits:
                if is_out:
                    driver_of[bit] = (str(instance), str(pin))
                    if is_ff and pin == "Q" and clk_domain is not None:
                        domain_q_nets.setdefault(clk_domain, set()).add(bit)
                else:
                    sinks_of.setdefault(bit, []).append((str(instance), str(pin)))
                    if (
                        is_ff
                        and pin not in _SCAN_CONTROL_PINS
                        and clk_domain is not None
                    ):
                        domain_d_nets.setdefault(clk_domain, set()).add(bit)

    def _forward_reachable(seeds: set[int]) -> set[int]:
        visited: set[int] = set(seeds)
        worklist = list(seeds)
        while worklist:
            net = worklist.pop()
            for inst, _pin in sinks_of.get(net, []):
                if inst in ff_instances:
                    continue  # FFs are state-holding: don't propagate through.
                cell = cells.get(inst)
                if not isinstance(cell, dict):
                    continue
                conns_c = cell.get("connections", {})
                if not isinstance(conns_c, dict):
                    continue
                ct = str(cell.get("type", ""))
                pd = cell.get("port_directions")
                pd_dict = (
                    {str(k): str(v) for k, v in pd.items()}
                    if isinstance(pd, dict)
                    else None
                )
                for opin, obits_raw in conns_c.items():
                    if _is_output_pin(ct, opin, pd_dict, cell_map):
                        for obit in _all_int_bits(obits_raw):
                            if obit not in visited:
                                visited.add(obit)
                                worklist.append(obit)
        return visited

    def _backward_reachable(seeds: set[int]) -> set[int]:
        visited: set[int] = set(seeds)
        worklist = list(seeds)
        while worklist:
            net = worklist.pop()
            drv = driver_of.get(net)
            if drv is None:
                continue
            inst, _pin = drv
            if inst in ff_instances:
                continue  # Don't traverse backward through FFs.
            cell = cells.get(inst)
            if not isinstance(cell, dict):
                continue
            conns_c = cell.get("connections", {})
            if not isinstance(conns_c, dict):
                continue
            ct = str(cell.get("type", ""))
            pd = cell.get("port_directions")
            pd_dict = (
                {str(k): str(v) for k, v in pd.items()}
                if isinstance(pd, dict)
                else None
            )
            for ipin, ibits_raw in conns_c.items():
                if not _is_output_pin(ct, ipin, pd_dict, cell_map):
                    for ibit in _all_int_bits(ibits_raw):
                        if ibit not in visited:
                            visited.add(ibit)
                            worklist.append(ibit)
        return visited

    # For each domain: forward-reachable from FF-Q (launch) and
    # backward-reachable from FF-D inputs + POs (capture).
    testable_nets: set[int] = set()
    for clk in clock_nets:
        launch = _forward_reachable(domain_q_nets.get(clk, set()))
        capture_seeds = domain_d_nets.get(clk, set()) | po_nets
        capture = _backward_reachable(capture_seeds)
        testable_nets |= launch & capture

    # Collect all nets that appear in the netlist.
    all_nets: set[int] = set()
    all_nets.update(pi_nets)
    all_nets.update(po_nets)
    all_nets.update(driver_of.keys())
    all_nets.update(sinks_of.keys())

    # Cross-domain = non-PI, non-PO, not testable in any domain.
    return all_nets - pi_nets - testable_nets


def tag_cross_domain_exclusions(
    generic_rows: list[dict[str, Any]],
    cross_domain_ids: set[int],
    existing_exclusions: dict[str, str],
) -> dict[str, str]:
    """Return updated exclusions dict with cross_domain entries added.

    Only tags fault sites whose yosys_net_id is in ``cross_domain_ids`` and
    are not already tagged with another exclusion.
    """
    if not cross_domain_ids:
        return existing_exclusions
    result = dict(existing_exclusions)
    for row in generic_rows:
        key = str(row.get("site_key", ""))
        yid = int(row.get("yosys_net_id", -1))
        if yid in cross_domain_ids and key not in result:
            result[key] = "cross_domain"
    return result
