"""Project manifest for hierarchical (block-as-top) wrapping + aggregation.

A *project* describes a chip as a set of independently-tested wrapped blocks plus
a thin SoC assembly used for the interconnect EXTEST and coverage aggregation
(see `docs/plans` / the hierarchical roadmap). Stage 1 consumes blocks that are
already scan-inserted + wrapped (the `wrap`/`scan` fields are forward-looking for
Stages 2/4) and an assembly netlist whose block *cores* are blackboxed.

Schema ``faultflow_project_v1``:

```json
{ "schema": "faultflow_project_v1", "name": "soc2", "cell_map_profile": "sky130",
  "base_config": "config.ofs",
  "blocks": [ {"name": "blkA", "top": "blkA",
               "generic_json": "soc2/blkA_scan.json",
               "scan_manifest": "soc2/blkA_manifest.json"} ],
  "interconnect": {"assembly_top": "soc2_top",
                   "assembly_netlist": "soc2/soc2_top.json",
                   "blackbox_instances": ["u_coreA", "u_coreB"]},
  "aggregation": {"policy": "disjoint_union"} }
```

Block fields are resolved relative to the manifest's directory.

Interconnect ``mode`` (default ``"comb"``, back-compat with the shape above): the
buffer-model wrapper is stateless, so its EXTEST is plain combinational ATPG on an
already-synthesized `assembly_netlist` with the block cores blackboxed.

``mode: "scan"`` targets the native, shiftable IEEE-1500 scan WBC instead: each
block's WBC ring is sequential (an internal shift FF per pin), so its EXTEST needs
the fused scan-EXTEST view (see `faultflow/scan/wbr_view.py`), and the assembly
netlist doesn't pre-exist -- it's composed at run time from a glue RTL + each
block's frozen JSON (see `faultflow/project/assemble.py`). Schema:

```json
{ "...": "...",
  "blocks": [ {"name": "blkA", "top": "alu_acc", "soc_instance": "u_a",
               "generic_json": "...", "scan_manifest": "..."} ],
  "interconnect": {"assembly_top": "soc_top", "mode": "scan",
                   "soc_rtl": "soc2/soc_glue.v",
                   "soc_wbr_si": "soc_wbr_si", "soc_wbr_so": "soc_wbr_so",
                   "soc_wbr_se": "soc_wbr_se", "clock_port": "clk"} }
```

``soc_instance`` is the block's instance name in ``soc_rtl`` (required, scan mode
only) -- `assemble_soc`/`compose_soc` key blocks by glue-RTL instance name, which is
generally not the same as the block's own ``name``/``top``. ``assembly_netlist`` /
``blackbox_instances`` are comb-mode-only and must be absent in scan mode; the
liberty file for glue synthesis comes from the project's `base_config` (`[design]
liberty`), not a manifest field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError

PROJECT_SCHEMA = "faultflow_project_v1"


class ProjectError(ConfigError):
    """A malformed or inconsistent project manifest."""


@dataclass(frozen=True)
class BlockSpec:
    name: str
    top: str
    generic_json: Path
    scan_manifest: Path
    # The block's instance name in interconnect.soc_rtl -- required for mode="scan"
    # (assemble_soc/compose_soc key blocks by glue-RTL instance name); unused for
    # mode="comb", where the assembly netlist is already synthesized/composed.
    soc_instance: str | None = None


@dataclass(frozen=True)
class InterconnectSpec:
    assembly_top: str
    mode: str = "comb"  # "comb" (buffer-model, back-compat) | "scan" (native WBC)
    # comb mode:
    assembly_netlist: Path | None = None
    blackbox_instances: tuple[str, ...] = ()
    # scan mode:
    soc_rtl: Path | None = None
    soc_wbr_si: str | None = None
    soc_wbr_so: str | None = None
    soc_wbr_se: str | None = None
    clock_port: str | None = None


@dataclass(frozen=True)
class ProjectManifest:
    name: str
    cell_map_profile: str
    base_config: Path
    blocks: tuple[BlockSpec, ...]
    interconnect: InterconnectSpec
    aggregation_policy: str = "disjoint_union"
    root: Path = field(default=Path("."))


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ProjectError(f"{where}: missing required field {key!r}")
    return obj[key]


def _resolve(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProjectError(f"{where}: expected a non-empty path string")
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def load_project(path: str | Path) -> ProjectManifest:
    """Parse + validate a project manifest. Paths resolve against its directory."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ProjectError(f"project manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectError(f"project manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError("project manifest root must be a JSON object")

    schema = data.get("schema")
    if schema != PROJECT_SCHEMA:
        raise ProjectError(
            f"unsupported project schema {schema!r}; expected {PROJECT_SCHEMA!r}"
        )
    root = manifest_path.resolve().parent

    raw_ic = _req(data, "interconnect", "project")
    if not isinstance(raw_ic, dict):
        raise ProjectError("project.interconnect must be an object")
    mode = str(raw_ic.get("mode", "comb"))
    if mode not in ("comb", "scan"):
        raise ProjectError(f"interconnect.mode must be 'comb' or 'scan', got {mode!r}")

    raw_blocks = _req(data, "blocks", "project")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ProjectError("project.blocks must be a non-empty list")
    blocks: list[BlockSpec] = []
    seen_tops: set[str] = set()
    seen_instances: set[str] = set()
    for i, raw in enumerate(raw_blocks):
        where = f"project.blocks[{i}]"
        if not isinstance(raw, dict):
            raise ProjectError(f"{where}: must be an object")
        top = str(_req(raw, "top", where))
        if top in seen_tops:
            raise ProjectError(f"{where}: duplicate block top {top!r}")
        seen_tops.add(top)
        soc_instance: str | None = None
        if mode == "scan":
            soc_instance = str(_req(raw, "soc_instance", where))
            if soc_instance in seen_instances:
                raise ProjectError(f"{where}: duplicate soc_instance {soc_instance!r}")
            seen_instances.add(soc_instance)
        blocks.append(
            BlockSpec(
                name=str(raw.get("name", top)),
                top=top,
                generic_json=_resolve(root, _req(raw, "generic_json", where), where),
                scan_manifest=_resolve(root, _req(raw, "scan_manifest", where), where),
                soc_instance=soc_instance,
            )
        )

    assembly_top = str(_req(raw_ic, "assembly_top", "project.interconnect"))
    if assembly_top in seen_tops:
        raise ProjectError(
            f"assembly_top {assembly_top!r} collides with a block top; "
            "the assembly must be a distinct top"
        )

    if mode == "scan":
        for forbidden in ("assembly_netlist", "blackbox_instances"):
            if forbidden in raw_ic:
                raise ProjectError(
                    f"interconnect.{forbidden} is comb-mode-only; "
                    "mode='scan' composes the assembly netlist at run time"
                )
        interconnect = InterconnectSpec(
            assembly_top=assembly_top,
            mode="scan",
            soc_rtl=_resolve(
                root,
                _req(raw_ic, "soc_rtl", "project.interconnect"),
                "project.interconnect",
            ),
            soc_wbr_si=str(_req(raw_ic, "soc_wbr_si", "project.interconnect")),
            soc_wbr_so=str(_req(raw_ic, "soc_wbr_so", "project.interconnect")),
            soc_wbr_se=str(_req(raw_ic, "soc_wbr_se", "project.interconnect")),
            clock_port=str(_req(raw_ic, "clock_port", "project.interconnect")),
        )
    else:
        for forbidden in ("soc_rtl", "soc_wbr_si", "soc_wbr_so", "soc_wbr_se"):
            if forbidden in raw_ic:
                raise ProjectError(
                    f"interconnect.{forbidden} is scan-mode-only "
                    "(interconnect.mode='scan')"
                )
        bb = raw_ic.get("blackbox_instances", [])
        if not isinstance(bb, list) or not all(isinstance(x, str) for x in bb):
            raise ProjectError(
                "interconnect.blackbox_instances must be a list of strings"
            )
        interconnect = InterconnectSpec(
            assembly_top=assembly_top,
            mode="comb",
            assembly_netlist=_resolve(
                root,
                _req(raw_ic, "assembly_netlist", "project.interconnect"),
                "project.interconnect",
            ),
            blackbox_instances=tuple(bb),
        )

    agg = data.get("aggregation", {})
    policy = (
        str(agg.get("policy", "disjoint_union"))
        if isinstance(agg, dict)
        else "disjoint_union"
    )

    return ProjectManifest(
        name=str(data.get("name", manifest_path.stem)),
        cell_map_profile=str(data.get("cell_map_profile", "sky130")),
        base_config=_resolve(root, data.get("base_config", "config.ofs"), "project"),
        blocks=tuple(blocks),
        interconnect=interconnect,
        aggregation_policy=policy,
        root=root,
    )
