"""Coverage of an autoMBIST design by instance category.

The manifest sorts every instance of the design into a category -- memory,
mbist_controller, self_repair, diagnosis, repair_remap and, for a design
wrapped for JTAG access, jtag_tap, ijtag_sib, ijtag_tdr, ijtag_scan_mux. Each
spliced block's cells carry its instance path as a prefix (`<path>__<cell>`,
compose_soc), so a fault belongs to the instance of the cell it sits on:

  branch   the cell whose input pin it is
  stem     the cell driving the net; a scan flop's pseudo-input (__ppi_<ff>)
           stands for the flop
  pseudo   the flop it names

Cells the scan flow adds carry the name of what they serve: a flop's
`$ff..._<flop>` helpers, and a blackbox's tie (`faultflow_blackbox`) and
`$bbsink_<instance>_...` readers. What no instance owns -- the wrapper's own
logic, its top-level ports, the constant nets -- counts as GLUE, so the
categories always add up to the campaign's totals.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from faultflow.integrations.autombist import AutombistManifest
from faultflow.scan.atpg_view import (
    BLACKBOX_INSTANCE_ATTR,
    BLACKBOX_SINK_PREFIX,
    PPI_PREFIX,
    PPO_PREFIX,
)
from faultflow.scan.stitch import _load_json, _lookup_cell, _top_module

GLUE = "glue"
# Graded by scan patterns like any other logic; see category_coverage.
JTAG_CATEGORIES = frozenset({"jtag_tap", "ijtag_sib", "ijtag_tdr", "ijtag_scan_mux"})


def instance_categories(manifest: AutombistManifest) -> dict[str, str]:
    """Instance path -> category: the wrapped design's instances for a design
    wrapped for JTAG access, the generated design's otherwise."""
    if manifest.test_access is not None:
        return {
            inst.hierarchical_path: inst.category
            for inst in manifest.test_access.instances
        }
    return {inst.hierarchical_path: inst.category for inst in manifest.instances}


class _Owners:
    """Resolves a netlist cell to the instance whose logic it is."""

    def __init__(self, module: dict[str, Any], instances: Iterable[str]) -> None:
        self._cells: dict[str, Any] = module.get("cells", {})
        # Longest first: warptap_sib_x_inst_0 before warptap_sib_x.
        self._paths = sorted(instances, key=len, reverse=True)

    def of_cell(self, name: str) -> str | None:
        cell = self._cells.get(name)
        if isinstance(cell, dict):
            owner = cell.get("attributes", {}).get(BLACKBOX_INSTANCE_ATTR)
            if isinstance(owner, str):
                return owner
        if name.startswith(BLACKBOX_SINK_PREFIX):
            rest = name[len(BLACKBOX_SINK_PREFIX) :]
            return next((p for p in self._paths if rest.startswith(p + "_")), None)
        for path in self._paths:
            if name == path or name.startswith(path + "__"):
                return path
        if name.startswith("$"):
            # A helper the scan flow added for a flop, named after it.
            return next((p for p in self._paths if f"_{p}__" in name), None)
        return None

    def of_port(self, port: str) -> str | None:
        """A scan flop's pseudo-port stands for the flop; any other port is
        the design's own boundary."""
        for prefix in (PPI_PREFIX, PPO_PREFIX):
            if port.startswith(prefix):
                return self.of_cell(port[len(prefix) :])
        return None


def _drivers(module: dict[str, Any], cell_map: dict[str, Any]) -> dict[int, str]:
    """Net -> the cell driving it, or "port:<name>" for an input port. A
    library cell's output pins come from the cell map: Yosys records port
    directions only for cells whose type it knows."""
    drivers: dict[int, str] = {}
    for name, cell in module.get("cells", {}).items():
        if not isinstance(cell, dict):
            continue
        directions = cell.get("port_directions") or {}
        if directions:
            outputs = [p for p, d in directions.items() if d == "output"]
        else:
            match = _lookup_cell(cell_map, str(cell.get("type", "")))
            outs = match[1].get("outputs", {}) if match is not None else {}
            outputs = [str(p) for p in outs.values()] if isinstance(outs, dict) else []
        for pin in outputs:
            for bit in cell.get("connections", {}).get(pin, []):
                if isinstance(bit, int):
                    drivers[bit] = str(name)
    for name, port in module.get("ports", {}).items():
        if isinstance(port, dict) and port.get("direction") == "input":
            for bit in port.get("bits", []):
                if isinstance(bit, int):
                    drivers.setdefault(bit, f"port:{name}")
    return drivers


def _owner(
    site_key: str, net_id: int, owners: _Owners, drivers: dict[int, str]
) -> str | None:
    if site_key.startswith("pseudo:"):
        return owners.of_cell(site_key.split(":", 2)[2])
    if ":branch:" in site_key:
        # net:<id>:branch:<consumer>:<pin>. A Yosys cell name may itself
        # hold colons; a pin name never does.
        consumer = site_key.split(":branch:", 1)[1].rsplit(":", 1)[0]
        return owners.of_cell(consumer) or owners.of_port(consumer)
    driver = drivers.get(net_id)
    if driver is None:
        return None
    if driver.startswith("port:"):
        return owners.of_port(driver[len("port:") :])
    return owners.of_cell(driver)


def category_coverage(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    netlist_json: Path,
    cell_map_json: Path,
    top: str,
    categories: dict[str, str],
    jtag_detected: frozenset[int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per category: detected, denominator, blackbox_unresolved and
    hold_unresolved, counted as db.summary counts them, over the faults of
    `campaign_id`. `netlist_json`
    is the netlist the campaign's fault sites name: the scan ATPG view for a
    scan campaign. The categories -- GLUE included -- add up to the summary.

    Scan patterns grade the TAP and the IJTAG network like any other logic:
    their flops are scanned -- unless [scan] nonscan_cells leaves them to JTAG
    (exclusion jtag), out of these denominators. `jtag_detected` (fault ids
    ff.py jtag's network-integrity program detected) adds combined_detected,
    combined_denominator and combined_coverage_percent: scan and JTAG credit
    together, a scan-redundant fault JTAG detects counting as detected, and a
    fault left to JTAG counting whether JTAG detects it or not."""
    _, module = _top_module(_load_json(netlist_json), top)
    owners = _Owners(module, categories)
    drivers = _drivers(module, _load_json(cell_map_json))
    counts: dict[str, dict[str, Any]] = {}
    # The fault id is read only to match JTAG detections. A fault scan leaves to
    # JTAG ([scan] nonscan_cells) is in the combined denominator either way.
    jtag_columns = (
        ", id, (exclusion IN ('none', 'jtag') AND collapsed_into IS NULL) AS eligible,"
        " (exclusion = 'jtag' AND collapsed_into IS NULL) AS left_to_jtag"
        if jtag_detected is not None
        else ""
    )
    for row in conn.execute(
        f"""
        SELECT fault_site_key, net_id,
          (exclusion = 'none' AND collapsed_into IS NULL
           AND status != 'redundant') AS counted,
          (status = 'detected' AND exclusion = 'none'
           AND collapsed_into IS NULL) AS detected,
          (blackbox_unresolved = 1 AND exclusion = 'none'
           AND collapsed_into IS NULL AND status != 'detected') AS unresolved,
          (hold_unresolved = 1 AND exclusion = 'none'
           AND collapsed_into IS NULL AND status != 'detected') AS held
          {jtag_columns}
        FROM faults
        WHERE campaign_id = ?
        """,
        (campaign_id,),
    ):
        owner = _owner(str(row[0]), int(row[1]), owners, drivers)
        category = categories.get(owner, GLUE) if owner is not None else GLUE
        entry = counts.setdefault(
            category,
            {
                "detected": 0,
                "denominator": 0,
                "blackbox_unresolved": 0,
                "hold_unresolved": 0,
            },
        )
        entry["denominator"] += int(row[2])
        entry["detected"] += int(row[3])
        entry["blackbox_unresolved"] += int(row[4])
        entry["hold_unresolved"] += int(row[5])
        if jtag_detected is not None:
            by_jtag = int(row[6]) in jtag_detected and bool(row[7])
            entry.setdefault("combined_detected", 0)
            entry.setdefault("combined_denominator", 0)
            entry["combined_detected"] += int(bool(row[3]) or by_jtag)
            entry["combined_denominator"] += int(
                bool(row[2]) or by_jtag or bool(row[8])
            )
    for entry in counts.values():
        denominator = entry["denominator"]
        entry["coverage_percent"] = (
            100.0 * entry["detected"] / denominator if denominator else None
        )
        if "combined_detected" in entry:
            combined = entry["combined_denominator"]
            entry["combined_coverage_percent"] = (
                100.0 * entry["combined_detected"] / combined if combined else None
            )
    return dict(sorted(counts.items()))
