"""Which test grades which faults: the coverage report's ``graded_by`` block.

Scan ATPG grades the core and the inserted logic that is ordinary logic -- an IEEE 1500
wrapper's boundary cells, the MBIST controller and shell. Test instruments are tested by
their own programs, as industrial flows do: a JTAG TAP and the IJTAG network by ``ff.py
jtag`` (the faults scan leaves to it are ``excluded_jtag``), an IEEE 1500 WSP by
``ff.py wsp`` (``excluded_wsp``); memories by MBIST, which scan never grades.

The block counts each population and what its own test detected -- ``None`` where that
test hasn't run, which is not zero -- and, once any has run, the chip's coverage with
them all: every fault of the scan's, JTAG's and the WSP's populations, detected by the
test that grades it or by any other that happens to (a fault scan leaves undetected and
JTAG detects), and redundant only if scan proved it and none detected it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence


def graded_by_block(
    conn: sqlite3.Connection,
    campaign_id: int,
    summary: Mapping[str, Any],
    *,
    memories: Sequence[str] = (),
) -> dict[str, Any]:
    """The report's ``graded_by`` block for the campaign. ``summary`` is its own
    db.summary; ``memories`` the blackboxed memory instances MBIST tests."""
    from faultflow.jtag import store as jtag_store
    from faultflow.wrap import wsp_grade

    jtag_run = jtag_store.latest_run(conn, campaign_id)
    wsp_run = wsp_grade.latest_run(conn, campaign_id)
    joins, by_jtag, by_wsp = [], "0", "0"
    params: list[int] = []
    if jtag_run is not None:
        joins.append(
            "LEFT JOIN jtag_detections j ON j.fault_id = f.id AND j.run_id = ?"
        )
        by_jtag = "(j.fault_id IS NOT NULL)"
        params.append(int(jtag_run["id"]))
    if wsp_run is not None:
        joins.append("LEFT JOIN wsp_detections w ON w.fault_id = f.id AND w.run_id = ?")
        by_wsp = "(w.fault_id IS NOT NULL)"
        params.append(int(wsp_run["id"]))
    rows = conn.execute(
        f"""
        SELECT f.exclusion, f.status, {by_jtag} AS by_jtag, {by_wsp} AS by_wsp,
               COUNT(*)
        FROM faults f {' '.join(joins)}
        WHERE f.campaign_id = ? AND f.collapsed_into IS NULL
        GROUP BY f.exclusion, f.status, by_jtag, by_wsp
        """,
        (*params, campaign_id),
    ).fetchall()

    faults = {"none": 0, "jtag": 0, "wsp": 0, "reset": 0}
    scan_detected = jtag_detected = wsp_detected = 0
    structural = detected = redundant = 0
    for exclusion, status, jtag_hit, wsp_hit, count in rows:
        count = int(count)
        if exclusion in faults:
            faults[exclusion] += count
        if exclusion == "none" and status == "detected":
            scan_detected += count
        if exclusion == "jtag" and jtag_hit:
            jtag_detected += count
        if exclusion == "wsp" and wsp_hit:
            wsp_detected += count
        if exclusion in ("none", "jtag", "wsp"):
            structural += count
            hit = bool(jtag_hit or wsp_hit)
            if status == "detected" or hit:
                detected += count
            elif status == "redundant":
                redundant += count
    for key, name in (("jtag", "excluded_jtag"), ("wsp", "excluded_wsp")):
        if faults[key] != int(summary.get(name, 0)):
            raise RuntimeError(
                f"graded_by invariant failed: {faults[key]} {key} faults, "
                f"summary {name} {summary.get(name, 0)}"
            )
    block: dict[str, Any] = {
        "scan": {"faults": faults["none"], "detected": scan_detected},
        "jtag": {
            "faults": faults["jtag"],
            "detected": jtag_detected if jtag_run is not None else None,
            "graded": jtag_run is not None,
        },
        "wsp": {
            "faults": faults["wsp"],
            "detected": wsp_detected if wsp_run is not None else None,
            "graded": wsp_run is not None,
        },
        "reset": {"faults": faults["reset"]},
        "memories": {"instances": list(memories)},
        "combined": None,
    }
    if jtag_run is not None or wsp_run is not None:
        denominator = structural - redundant
        block["combined"] = {
            "structural_eligible": structural,
            "denominator": denominator,
            "detected": detected,
            "redundant": redundant,
            "test_coverage_percent": (
                100.0 * detected / denominator if denominator else None
            ),
        }
    return block


def graded_by_lines(block: Mapping[str, Any]) -> list[str]:
    """The report's text for its ``graded_by`` block; a population with no fault has
    no line."""
    lines = ["graded by (faults, detected by its own test):"]
    scan = block["scan"]
    lines.append(f"  scan ATPG:   {scan['faults']} faults, {scan['detected']} detected")
    for key, test in (("jtag", "ff.py jtag"), ("wsp", "ff.py wsp")):
        entry = block[key]
        if not entry["faults"]:
            continue
        result = (
            f"{entry['detected']} detected" if entry["graded"] else f"not run ({test})"
        )
        lines.append(f"  {key + ':':12s} {entry['faults']} faults left to it, {result}")
    if block["reset"]["faults"]:
        lines.append(
            f"  reset:       {block['reset']['faults']} faults of settled non-scan "
            "flops (resets)"
        )
    memories = block["memories"]["instances"]
    if memories:
        lines.append(
            f"  memories:    {', '.join(memories)}: tested by MBIST, not graded here"
        )
    combined = block["combined"]
    if combined is not None:
        percent = combined["test_coverage_percent"]
        lines.append(
            f"  all tests:   {combined['detected']} / {combined['denominator']} "
            + ("n/a" if percent is None else f"{percent:.3f}%")
        )
    return lines
