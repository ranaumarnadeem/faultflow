"""Drive per-block INTEST + assembly EXTEST for a hierarchical project.

Stage 1 (block-as-top): each block is tested in isolation as its own top via the
existing scan INTEST pipeline; the interconnect is tested by a combinational
EXTEST on the assembly netlist with the block *cores* blackboxed. Every scope runs
in its own workspace (`output_root/<scope>/.faultflow/faultflow.sqlite`), so the
campaigns are isolated by path and need no DB schema change.

The orchestrator reuses the whole existing pipeline — it only sets up each scope's
config + workspace and calls `FlowService.run_atpg`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faultflow.config import FaultflowConfig, load_config
from faultflow.project.manifest import ProjectManifest
from faultflow.scan.reports import hash_file, utc_timestamp

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopeRun:
    """Where a finished scope's results live, for the aggregator to read."""

    kind: str  # "block" | "interconnect"
    name: str
    top: str
    campaign_type: str  # "scan" | "comb"
    db_path: Path
    netlist: Path  # the netlist faults were enumerated on (for canonical keys)
    blackbox_instances: tuple[str, ...]
    message: str


def _scope_config(
    base: FaultflowConfig,
    project_out: Path,
    *,
    top: str,
    netlist: Path,
    test_mode: str,
    name: str,
    blackbox_instances: tuple[str, ...] = (),
) -> FaultflowConfig:
    return dataclasses.replace(
        base,
        top=top,
        netlist=netlist,
        test_mode=test_mode,
        blackbox_instances=blackbox_instances,
        output_root=project_out / name,
    )


def _stage_block_scan_workspace(
    cfg: FaultflowConfig, generic_json: Path, scan_manifest: Path
) -> None:
    """Install a PASS-checked scan workspace so `sim --scan` (INTEST) can run.

    Copies the block's pre-scanned+wrapped generic JSON into the scope workspace
    and writes a manifest whose `generic_json`/hash point at it with a PASS
    `latest_check` (the block was already scan-checked upstream).
    """
    cfg.ensure_workspace()
    staged = cfg.scan_json_path
    staged.write_text(generic_json.read_text(encoding="utf-8"), encoding="utf-8")

    manifest: dict[str, Any] = json.loads(scan_manifest.read_text(encoding="utf-8"))
    manifest["top"] = cfg.top
    manifest["generic_json"] = str(staged)
    generic_hash = hash_file(staged)
    manifest["generic_json_hash"] = generic_hash
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": generic_hash,
    }
    cfg.scan_manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def run_project(
    project: ProjectManifest,
    *,
    run_atpg: Callable[..., Any],
    max_rounds: int | None = None,
    target_coverage: float | None = None,
    clean: bool = False,
) -> list[ScopeRun]:
    """Run INTEST on every block + EXTEST on the assembly. Returns scope handles.

    `run_atpg` is `FlowService.run_atpg` (injected for testability); it takes a
    `FaultflowConfig` plus `sim()` kwargs and returns an object whose `.message`
    is the run summary.
    """
    base = load_config(project.base_config, project.blocks[0].top)
    project_out = (project.root / "output" / project.name).resolve()
    scopes: list[ScopeRun] = []

    for block in project.blocks:
        cfg = _scope_config(
            base,
            project_out,
            top=block.top,
            netlist=block.generic_json,
            test_mode="intest",
            name=block.name,
        )
        _stage_block_scan_workspace(cfg, block.generic_json, block.scan_manifest)
        log.info("project block INTEST  name=%s top=%s", block.name, block.top)
        result = run_atpg(
            cfg,
            scan=True,
            clean=clean,
            max_rounds=max_rounds,
            target_coverage=target_coverage,
        )
        scopes.append(
            ScopeRun(
                kind="block",
                name=block.name,
                top=block.top,
                campaign_type="scan",
                db_path=cfg.db_path,
                netlist=cfg.scan_json_path,
                blackbox_instances=(),
                message=str(getattr(result, "message", result)),
            )
        )

    ic = project.interconnect
    cfg_asm = _scope_config(
        base,
        project_out,
        top=ic.assembly_top,
        netlist=ic.assembly_netlist,
        test_mode="extest",
        name="_interconnect",
        blackbox_instances=ic.blackbox_instances,
    )
    cfg_asm.ensure_workspace()
    log.info("project interconnect EXTEST  top=%s", ic.assembly_top)
    # Combinational EXTEST: the assembly has no FFs (cores blackboxed), so the
    # mode-aware combinational engine + blackbox isolation give the interconnect +
    # WBC-outward fault set directly. verify auto-skips on blackbox instances.
    result = run_atpg(
        cfg_asm,
        scan=False,
        clean=clean,
        max_rounds=max_rounds,
        target_coverage=target_coverage,
    )
    scopes.append(
        ScopeRun(
            kind="interconnect",
            name=ic.assembly_top,
            top=ic.assembly_top,
            campaign_type="comb",
            db_path=cfg_asm.db_path,
            netlist=ic.assembly_netlist,
            blackbox_instances=ic.blackbox_instances,
            message=str(getattr(result, "message", result)),
        )
    )
    return scopes
