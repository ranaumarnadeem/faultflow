"""A ``faultflow_project_v2`` SoC, built for its tests: its blocks composed through
their glue, its scan chains traced, its wrappers recorded, and its scan workspace
staged as ``ff.py scan`` would stage a block's.

Each block was wrapped and scanned on its own (``ff.py scan`` with ``[wrap]
enabled``), and its frozen scan JSON is spliced in unchanged
(faultflow.project.assemble): the SoC is never re-synthesized across a block's
boundary. :func:`build_soc` composes it, every cell tagged with its block, and
returns each block's net remap, which traces a fault of the SoC back to its blocks'
(faultflow.project.identity). The SoC's scan manifest is traced on its cells
(faultflow.project.chip) and carries one wrapper record for every block
(faultflow.project.soc_wrapper); its sky130 Verilog is the scan techmap of the
composed cells, as a block's is.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faultflow.config import FaultflowConfig
from faultflow.control_trace import Netlist
from faultflow.project.assemble import assemble_soc
from faultflow.project.chip import chip_manifest
from faultflow.project.manifest import BlockSpec, ProjectError, SocProject
from faultflow.project.soc_wrapper import soc_wrapper
from faultflow.retarget import retarget_patterns
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.reports import hash_file
from faultflow.scan.techmap import write_scan_techmap
from faultflow.scan.yosys import run_scan_techmap
from faultflow.wrap.record import mode_holds

# Where each block's INTEST patterns are exported, in its scope's output directory:
# what retargeting onto the SoC reads.
INTEST_PATTERNS = "intest_patterns.json"


@dataclass(frozen=True)
class SocBuild:
    """The composed SoC (generic JSON), its scope's config (workspace staged), and
    per block instance: its name, its own scan manifest, and its net remap."""

    composed: Path
    cfg: FaultflowConfig
    blocks: dict[str, tuple[str, dict[str, Any]]]
    remaps: dict[str, dict[int, int | str]]


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProjectError(f"{path} isn't a JSON object")
    return data


def project_output(project: SocProject) -> Path:
    """Where ``ff.py project`` puts `project`'s scopes and its report."""
    return (project.root / "output" / project.name).resolve()


def soc_scope(project: SocProject, base: FaultflowConfig, out: Path) -> FaultflowConfig:
    """The SoC's scope under `out`: its tests hold the inputs ``soc.hold`` names,
    beside the base config's ``[scan] hold``."""
    hold = dict(base.scan.hold)
    for port, value in project.soc.hold:
        if hold.setdefault(port, value) != value:
            raise ProjectError(
                f"soc.hold holds {port} at {value}, the base config's [scan] hold "
                f"at {hold[port]}"
            )
    cfg = dataclasses.replace(
        base,
        top=project.soc.top,
        output_root=out / "soc",
        test_mode="functional",
        scan=dataclasses.replace(base.scan, hold=tuple(hold.items())),
    )
    return dataclasses.replace(cfg, netlist=cfg.scan_json_path)


def block_scope(base: FaultflowConfig, out: Path, block: BlockSpec) -> FaultflowConfig:
    """`block`'s INTEST scope under `out`."""
    cfg = dataclasses.replace(
        base, top=block.top, output_root=out / block.name, test_mode="intest"
    )
    return dataclasses.replace(cfg, netlist=cfg.scan_json_path)


def build_soc(project: SocProject, base: FaultflowConfig, out: Path) -> SocBuild:
    """Compose `project`'s SoC under `out`, and stage its scope's scan workspace:
    the scan manifest (chains, wrappers) and the sky130 Verilog."""
    if base.liberty is None:
        raise ProjectError("the SoC glue needs [design] liberty to synthesize")
    cfg = soc_scope(project, base, out)
    cfg.ensure_workspace()
    blocks: dict[str, tuple[str, dict[str, Any]]] = {}
    paths: dict[str, Path] = {}
    modules: dict[str, str] = {}
    names: dict[str, str] = {}
    for block in project.blocks:
        manifest = _load(block.scan_manifest)
        if not isinstance(manifest.get("wrapper"), dict):
            raise ProjectError(
                f"block {block.name}'s scan manifest records no wrapper: scan it "
                "with [wrap] enabled"
            )
        for key in ("compression", "compaction"):
            if isinstance(manifest.get(key), dict) and manifest[key].get("enabled"):
                raise ProjectError(f"block {block.name} has scan {key}")
        blocks[block.soc_instance] = (block.name, manifest)
        paths[block.soc_instance] = block.generic_json
        modules[block.soc_instance] = block.top
        names[block.soc_instance] = block.name
    remaps: dict[str, dict[int, int | str]] = {}
    composed = cfg.scan_json_path
    assemble_soc(
        soc_rtl=project.soc.rtl,
        soc_top=project.soc.top,
        liberty=base.liberty,
        blocks=paths,
        block_module=modules,
        output_json=composed,
        workdir=cfg.workspace_dir / "assemble",
        block_names=names,
        tag_blocks=True,
        remaps=remaps,
    )
    data = _load(composed)
    module = data["modules"][project.soc.top]
    cell_map = _load(resolve_scan_cell_map(cfg))
    # Every pin's direction on the cell, as the cell map gives the glue's: what
    # drives what, wherever the composed netlist is read.
    trace = Netlist(module, cell_map)
    for name, cell in module["cells"].items():
        entry = trace.entry(name)
        directions = cell.setdefault("port_directions", {})
        for pin in cell.get("connections", {}):
            directions.setdefault(pin, trace.direction(name, pin, entry))
    composed.write_text(json.dumps(data, indent=2), encoding="utf-8")
    original = {
        f"{instance}__{record['instance']}": str(record["original_type"])
        for instance, (_, manifest) in blocks.items()
        for record in manifest["cells"]
    }
    manifest = chip_manifest(
        module, project.soc.top, cell_map, composed, original_types=original
    )
    manifest["wrapper"] = soc_wrapper(
        module,
        cell_map,
        {instance: (name, m["wrapper"]) for instance, (name, m) in blocks.items()},
        remaps,
        manifest["chains"],
    )
    techmap = write_scan_techmap(cfg.generated_scripts_dir / "faultflow_scanff_map.v")
    verilog = run_scan_techmap(
        generic_json=composed,
        techmap_verilog=techmap,
        output_verilog=cfg.scan_verilog_path,
        top=project.soc.top,
        log_path=cfg.logs_dir / "yosys_scan.log",
        script_path=cfg.generated_scripts_dir / "yosys_scan.ys",
    )
    manifest.update(
        {
            "techmap_verilog": str(techmap),
            "sky130_verilog": str(verilog),
            "generic_json_hash": hash_file(composed),
        }
    )
    cfg.scan_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.scan_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return SocBuild(composed, cfg, blocks, remaps)


@dataclass(frozen=True)
class SocScopes:
    """A finished project's scopes: each block's INTEST (by instance) and the SoC's
    EXTEST, and the SoC as built."""

    blocks: dict[str, FaultflowConfig]
    soc: FaultflowConfig
    build: SocBuild


def _stage_block(cfg: FaultflowConfig, generic_json: Path, manifest_path: Path) -> None:
    """Install a block's scanned netlist and scan manifest in its scope's workspace,
    as ``ff.py scan`` and ``scan-check`` left them: the block must have passed its
    scan-check."""
    cfg.ensure_workspace()
    manifest = _load(manifest_path)
    check = manifest.get("latest_check")
    if not isinstance(check, dict) or check.get("status") != "PASS":
        raise ProjectError(f"{manifest_path}: run scan-check on the block first")
    staged = cfg.scan_json_path
    staged.write_bytes(generic_json.read_bytes())
    digest = hash_file(staged)
    manifest.update(
        {
            "top": cfg.top,
            "generic_json": str(staged),
            "generic_json_hash": digest,
            "latest_check": {**check, "generic_json_hash": digest},
        }
    )
    cfg.scan_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.scan_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_soc_project(
    project: SocProject,
    base: FaultflowConfig,
    *,
    run_atpg: Callable[..., Any],
    check_scan: Callable[[FaultflowConfig], Any],
    out: Path,
    max_rounds: int | None = None,
    target_coverage: float | None = None,
    clean: bool = False,
    soc_patterns: Path | None = None,
) -> SocScopes:
    """Build the SoC, scan-check it, then run each block's INTEST, each in its own
    workspace under `out`, its patterns exported there (INTEST_PATTERNS) for
    :func:`retarget_block`, and the SoC's EXTEST, its patterns exported to
    `soc_patterns` when given. `run_atpg` and `check_scan` are the FlowService's."""
    build = build_soc(project, base, out)
    check_scan(build.cfg)
    options: dict[str, Any] = {
        "scan": True,
        "clean": clean,
        "max_rounds": max_rounds,
        "target_coverage": target_coverage,
    }
    blocks: dict[str, FaultflowConfig] = {}
    for block in project.blocks:
        cfg = block_scope(base, out, block)
        _stage_block(cfg, block.generic_json, block.scan_manifest)
        run_atpg(cfg, **options, export_patterns=cfg.output_dir / INTEST_PATTERNS)
        blocks[block.soc_instance] = cfg
    soc = dataclasses.replace(build.cfg, test_mode="extest")
    if soc_patterns is not None:
        options["export_patterns"] = soc_patterns
    run_atpg(soc, **options)
    return SocScopes(blocks, soc, build)


def _core() -> Any:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    if core is None:
        raise ProjectError("the C++ extension _faultflow_core is required")
    return core


def block_sites(scopes: SocScopes) -> dict[str, Any]:
    """Per block instance, its fault sites as faultflow.project.identity traces
    them into the SoC (BlockSites): its scanned netlist's, its input nets, and its
    net remap."""
    from faultflow.project.identity import BlockSites

    core = _core()
    cell_map = str(resolve_scan_cell_map(scopes.soc))
    unsupported = scopes.soc.simulation.unsupported_cells
    sites: dict[str, Any] = {}
    for instance, cfg in scopes.blocks.items():
        module = _load(cfg.scan_json_path)["modules"][cfg.top]
        rows = tuple(
            dict(row)
            for row in core.list_site_keys(
                str(cfg.scan_json_path), cell_map, unsupported, []
            )
        )
        inputs = frozenset(
            bit
            for port in module["ports"].values()
            if port["direction"] == "input"
            for bit in port["bits"]
            if isinstance(bit, int)
        )
        name = scopes.build.blocks[instance][0]
        sites[instance] = BlockSites(name, rows, inputs, scopes.build.remaps[instance])
    return sites


def soc_chip_coverage(project: SocProject, scopes: SocScopes) -> Any:
    """The chip number over the project's scopes (faultflow.project.soc_aggregate),
    each SoC fault site traced to the block faults it stands for
    (faultflow.project.identity)."""
    from faultflow.project.identity import merged_inputs, soc_identities, stem_key
    from faultflow.project.soc_aggregate import Scope, aggregate_soc

    core = _core()
    soc = scopes.soc
    cell_map = str(resolve_scan_cell_map(soc))
    unsupported = soc.simulation.unsupported_cells
    graybox = soc.intermediate_dir / f"{project.soc.top}_extest.json"
    graybox_module = _load(graybox)["modules"][project.soc.top]
    stubs = sorted(
        name
        for name, cell in graybox_module["cells"].items()
        if cell.get("type") == "$faultflow_core"
    )
    soc_rows = list(core.list_site_keys(str(graybox), cell_map, unsupported, stubs))
    sites = block_sites(scopes)
    tied: list[tuple[str, str]] = []
    scope_list: list[Scope] = []
    for instance, cfg in scopes.blocks.items():
        site = sites[instance]
        constants = {b for b, net in site.remap.items() if not isinstance(net, int)}
        tied += [
            (site.name, str(row["site_key"]))
            for row in site.rows
            if int(row["yosys_net_id"]) in constants
        ]
        tied += [(site.name, stem_key(b)) for b in constants]

        def own(key: str, name: str = site.name) -> list[tuple[str, str]]:
            return [(name, key)]

        scope_list.append(Scope(site.name, cfg.db_path, "scan", own))
    identities = soc_identities(soc_rows, sites, stubs=stubs)
    scope_list.append(
        Scope(
            "soc",
            soc.db_path,
            "scan_extest",
            lambda key: identities.get(key, [("soc", key)]),
        )
    )
    # A block input port's stem the SoC splits into branches, and a block bit the
    # glue ties to a constant: no fault of the chip.
    accounted = [*tied, *merged_inputs(sites, identities)]
    return aggregate_soc(project.name, scope_list, tied=accounted)


def retarget_block(
    project: SocProject, base: FaultflowConfig, out: Path, name: str, dest: Path
) -> int:
    """Block `name`'s INTEST patterns, which the project's run under `out`
    exported, as the SoC takes them (faultflow.retarget), written to `dest`: how
    many."""
    block = next((b for b in project.blocks if b.name == name), None)
    if block is None:
        names = ", ".join(b.name for b in project.blocks)
        raise ProjectError(f"project {project.name} has no block {name!r} ({names})")
    cfg = block_scope(base, out, block)
    soc = soc_scope(project, base, out)
    patterns = cfg.output_dir / INTEST_PATTERNS
    for path in (patterns, soc.scan_manifest_path):
        if not path.exists():
            raise ProjectError(f"run the project first: no {path}")
    soc_manifest = _load(soc.scan_manifest_path)
    holds = dict(soc.scan.hold)
    for port, value in mode_holds(soc_manifest, "intest").items():
        if holds.setdefault(port, value) != value:
            raise ProjectError(
                f"[scan] hold holds {port} at {holds[port]}, INTEST at {value}"
            )
    retargeted = retarget_patterns(
        json.loads(patterns.read_text(encoding="utf-8")),
        instance=block.soc_instance,
        block_manifest=_load(cfg.scan_manifest_path),
        block_module=_load(cfg.scan_json_path)["modules"][block.top],
        soc_manifest=soc_manifest,
        soc_module=_load(soc.scan_json_path)["modules"][project.soc.top],
        cell_map=_load(resolve_scan_cell_map(soc)),
        holds=holds,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(retargeted, indent=2) + "\n", encoding="utf-8")
    return len(retargeted)
