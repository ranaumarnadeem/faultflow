"""OT _preflight subprocess wrapper: reconvergence analysis without TPI insertion."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightData:
    """Reconvergence analysis output from OT _preflight.

    fanout_yosys_ids: stem net IDs of reconvergent fanouts, an ATPG ordering
    hint. Never a redundancy verdict: OT's reconvergence records are purely
    structural (two branches of a stem meet again), and a reconvergent stem's
    faults -- and a single branch's, which reaches one reader -- can be
    testable. Only a SAT UNSAT makes a fault redundant.
    """

    fanout_yosys_ids: frozenset[int]


def run_preflight(
    netlist_json: Path,
    output_dir: Path,
    opentest_bin: str,
    tech: str,
) -> PreflightData | None:
    """Run ``opentest --yosys _preflight`` and parse the reconvergence output.

    Returns None on any error (binary unavailable, subprocess failure, or parse
    error) — callers treat None as "no preflight data" and proceed unchanged.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        opentest_bin,
        "--yosys",
        "_preflight",
        "-i",
        str(netlist_json),
        "--scoap",
        "-r",
        "--reconv-algorithm",
        "advanced",
        "--tech",
        tech,
        "-o",
        str(output_dir),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("atpg   preflight subprocess error: %s", exc)
        return None

    if proc.returncode != 0:
        log.debug(
            "atpg   preflight returned exit %d; skipping reconvergence data",
            proc.returncode,
        )
        return None

    # Find the last line that starts with '{' — OT prints progress lines before
    # the final JSON summary.
    summary_line: str | None = None
    for line in reversed(proc.stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            summary_line = stripped
            break

    if summary_line is None:
        log.debug("atpg   preflight: no JSON summary line in stdout")
        return None

    try:
        summary = json.loads(summary_line)
    except json.JSONDecodeError as exc:
        log.debug("atpg   preflight: JSON parse error: %s", exc)
        return None

    if summary.get("status") != "ok":
        log.debug("atpg   preflight: status=%r", summary.get("status"))
        return None

    manifest_path = summary.get("manifest")
    fanout_ids: frozenset[int] = frozenset()
    if manifest_path:
        fanout_ids = _parse_manifest(Path(manifest_path))

    return PreflightData(fanout_yosys_ids=fanout_ids)


def _parse_manifest(path: Path) -> frozenset[int]:
    """Extract reconvergent fanout stem net IDs from tpi_manifest.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("atpg   preflight: manifest read error: %s", exc)
        return frozenset()

    ids: set[int] = set()
    hints = data.get("structural_hints", {})
    for item in hints.get("fanout_points", []):
        if not isinstance(item, dict):
            continue
        net_id = item.get("yosys_net_id")
        if isinstance(net_id, int):
            ids.add(net_id)
    return frozenset(ids)
