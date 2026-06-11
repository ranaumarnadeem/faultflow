from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faultflow.scan.stitch import ScanPlan, ScanStitchResult


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_file(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cell_row(cell: Any) -> dict[str, object]:
    return {
        "instance": cell.instance,
        "original_type": cell.original_type,
        "chain_index": cell.chain_index,
        "chain_position": cell.chain_position,
        "clock_net": cell.clock_net,
        "data_net": cell.data_net,
        "scan_in_net": cell.scan_in_net,
        "scan_enable_net": cell.scan_enable_net,
        "q_net": cell.q_net,
    }


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dict_list(value: object) -> list[dict[str, object]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def manifest_from_result(
    result: ScanStitchResult,
    source_json: Path,
    techmap_verilog: Path,
    sky130_verilog: Path | None,
    check_result: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "version": 1,
        "top": result.top,
        "source_json": str(source_json),
        "generic_json": str(result.output_json),
        "generic_json_hash": hash_file(result.output_json),
        "techmap_verilog": str(techmap_verilog),
        "sky130_verilog": str(sky130_verilog) if sky130_verilog is not None else None,
        "chain_order_policy": "sorted by instance name, not netlist order",
        "chain_count": result.chain_count,
        "cell_count": result.cell_count,
        "clock_net": result.clock_net,
        "scan_inputs": result.scan_inputs,
        "scan_outputs": result.scan_outputs,
        "scan_enable": result.scan_enable,
        "max_chain_length": max((chain.length for chain in result.chains), default=0),
        "min_chain_length": min((chain.length for chain in result.chains), default=0),
        "chains": [
            {
                "index": chain.index,
                "scan_in": chain.scan_in,
                "scan_out": chain.scan_out,
                "scan_in_net": chain.scan_in_net,
                "scan_out_net": chain.scan_out_net,
                "length": chain.length,
                "cells": [cell.instance for cell in chain.cells],
                "cell_records": [_cell_row(cell) for cell in chain.cells],
            }
            for chain in result.chains
        ],
        "cells": [_cell_row(cell) for cell in result.cells],
        "ineligible_ffs": [
            {
                "instance": ff.instance,
                "cell_type": ff.cell_type,
                "reason": ff.reason,
            }
            for ff in result.ineligible_ffs
        ],
        "latest_check": check_result,
    }


def dry_run_manifest(plan: ScanPlan) -> dict[str, object]:
    return {
        "version": 1,
        "top": plan.top,
        "chain_order_policy": "sorted by instance name, not netlist order",
        "chain_count": plan.chain_count,
        "cell_count": plan.cell_count,
        "clock_net": plan.clock_net,
        "scan_inputs": plan.scan_inputs,
        "scan_outputs": plan.scan_outputs,
        "scan_enable": plan.scan_enable,
        "chains": [
            {
                "index": chain.index,
                "scan_in": chain.scan_in,
                "scan_out": chain.scan_out,
                "length": chain.length,
                "cells": [cell.instance for cell in chain.cells],
            }
            for chain in plan.chains
        ],
        "ineligible_ffs": [
            {
                "instance": ff.instance,
                "cell_type": ff.cell_type,
                "reason": ff.reason,
            }
            for ff in plan.ineligible_ffs
        ],
    }


def load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_scan_chains(path: Path, manifest: dict[str, object]) -> None:
    lines = [
        f"top: {manifest.get('top')}",
        "chain order: sorted by instance name, not netlist order",
        f"chains: {manifest.get('chain_count')} cells: {manifest.get('cell_count')}",
    ]
    for chain in _dict_list(manifest.get("chains", [])):
        cells = _list(chain.get("cells", []))
        body = " -> ".join(str(cell) for cell in cells)
        lines.append(
            f"Chain {chain.get('index')} ({chain.get('length')} FFs): "
            f"{chain.get('scan_in')} -> {body} -> {chain.get('scan_out')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_scan_report(manifest: dict[str, object]) -> str:
    ineligible = _dict_list(manifest.get("ineligible_ffs", []))
    reasons = Counter(
        str(item.get("reason")) for item in ineligible if item.get("reason")
    )
    latest = manifest.get("latest_check")
    scan_inputs = _list(manifest.get("scan_inputs", []))
    scan_outputs = _list(manifest.get("scan_outputs", []))
    lines = [
        "faultflow scan report",
        f"top: {manifest.get('top')}",
        "chain order: sorted by instance name, not netlist order",
        f"chains: {manifest.get('chain_count')}",
        f"scan cells: {manifest.get('cell_count')}",
        f"clock net: {manifest.get('clock_net')}",
        f"scan enable: {manifest.get('scan_enable')}",
        f"scan inputs: {', '.join(str(x) for x in scan_inputs)}",
        f"scan outputs: {', '.join(str(x) for x in scan_outputs)}",
        f"generic json: {manifest.get('generic_json')}",
        f"generic json hash: {manifest.get('generic_json_hash')}",
        f"techmap: {manifest.get('techmap_verilog')}",
        f"sky130 verilog: {manifest.get('sky130_verilog')}",
        "",
        "chains:",
    ]
    for chain in _dict_list(manifest.get("chains", [])):
        cells = " -> ".join(str(cell) for cell in _list(chain.get("cells", [])))
        lines.append(
            f"  chain {chain.get('index')} length={chain.get('length')} "
            f"{chain.get('scan_in')} -> {cells} -> {chain.get('scan_out')}"
        )
    lines.extend(["", "ineligible FFs:"])
    if not ineligible:
        lines.append("  none")
    else:
        for reason, count in sorted(reasons.items()):
            lines.append(f"  {reason}: {count}")
        for item in ineligible:
            lines.append(
                f"  {item.get('instance')} {item.get('cell_type')} "
                f"{item.get('reason')}"
            )
    lines.extend(["", "latest check:"])
    if isinstance(latest, dict):
        lines.append(f"  status: {latest.get('status')}")
        lines.append(f"  timestamp: {latest.get('timestamp')}")
        for error in _list(latest.get("errors", [])):
            lines.append(f"  error: {error}")
        for warning in _list(latest.get("warnings", [])):
            lines.append(f"  warning: {warning}")
    else:
        lines.append("  not run")
    return "\n".join(lines) + "\n"


def write_scan_report(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(render_scan_report(manifest), encoding="utf-8")


def write_scan_artifacts(
    scan_dir: Path,
    manifest: dict[str, object],
) -> None:
    write_manifest(scan_dir / "scan_manifest.json", manifest)
    write_scan_chains(scan_dir / "scan_chains.txt", manifest)
    write_scan_report(scan_dir / "scan.rpt", manifest)


def format_dry_run(plan: ScanPlan) -> str:
    reasons = Counter(ff.reason for ff in plan.ineligible_ffs)
    lines = [
        f"scan dry-run top={plan.top}",
        "chain order: sorted by instance name, not netlist order",
        f"eligible_ffs={plan.cell_count}",
        f"ineligible_ffs={len(plan.ineligible_ffs)}",
        f"chains={plan.chain_count}",
        f"scan_enable={plan.scan_enable}",
        f"scan_inputs={','.join(plan.scan_inputs)}",
        f"scan_outputs={','.join(plan.scan_outputs)}",
    ]
    for reason, count in sorted(reasons.items()):
        lines.append(f"ineligible_reason {reason}={count}")
    for chain in plan.chains:
        cells = " -> ".join(cell.instance for cell in chain.cells)
        lines.append(
            f"chain {chain.index} length={chain.length}: "
            f"{chain.scan_in} -> {cells} -> {chain.scan_out}"
        )
    lines.append("dry-run wrote no scan artifacts")
    return "\n".join(lines)
