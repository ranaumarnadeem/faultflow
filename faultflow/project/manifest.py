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


@dataclass(frozen=True)
class InterconnectSpec:
    assembly_top: str
    assembly_netlist: Path
    blackbox_instances: tuple[str, ...]


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

    raw_blocks = _req(data, "blocks", "project")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ProjectError("project.blocks must be a non-empty list")
    blocks: list[BlockSpec] = []
    seen_tops: set[str] = set()
    for i, raw in enumerate(raw_blocks):
        where = f"project.blocks[{i}]"
        if not isinstance(raw, dict):
            raise ProjectError(f"{where}: must be an object")
        top = str(_req(raw, "top", where))
        if top in seen_tops:
            raise ProjectError(f"{where}: duplicate block top {top!r}")
        seen_tops.add(top)
        blocks.append(
            BlockSpec(
                name=str(raw.get("name", top)),
                top=top,
                generic_json=_resolve(root, _req(raw, "generic_json", where), where),
                scan_manifest=_resolve(root, _req(raw, "scan_manifest", where), where),
            )
        )

    raw_ic = _req(data, "interconnect", "project")
    if not isinstance(raw_ic, dict):
        raise ProjectError("project.interconnect must be an object")
    bb = raw_ic.get("blackbox_instances", [])
    if not isinstance(bb, list) or not all(isinstance(x, str) for x in bb):
        raise ProjectError("interconnect.blackbox_instances must be a list of strings")
    assembly_top = str(_req(raw_ic, "assembly_top", "project.interconnect"))
    if assembly_top in seen_tops:
        raise ProjectError(
            f"assembly_top {assembly_top!r} collides with a block top; "
            "the assembly must be a distinct top"
        )
    interconnect = InterconnectSpec(
        assembly_top=assembly_top,
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
