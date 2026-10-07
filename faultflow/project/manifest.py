"""The project manifest of a hierarchical SoC test: blocks wrapped with the IEEE
1500 wrapper ff.py wrap puts on, each scanned and scan-checked on its own, and the
SoC their glue composes (faultflow.project.soc_flow).

Schema ``faultflow_project_v2`` (see :func:`load_soc_project`). The abstract
``$wbc_*`` wrapper's ``faultflow_project_v1`` (an ``interconnect`` of mode
``comb`` or ``scan``) is refused, with what to write instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError

# The retired abstract wrapper's project schema: named only to refuse it.
_V1_SCHEMA = "faultflow_project_v1"


class ProjectError(ConfigError):
    """A malformed or inconsistent project manifest."""


@dataclass(frozen=True)
class BlockSpec:
    """A block: its name, its top module, what ``ff.py scan`` wrote for it (scan
    JSON and scan manifest), and its instance in the SoC glue."""

    name: str
    top: str
    generic_json: Path
    scan_manifest: Path
    soc_instance: str


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ProjectError(f"{where}: missing required field {key!r}")
    return obj[key]


def _resolve(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProjectError(f"{where}: expected a non-empty path string")
    p = Path(value)
    return p if p.is_absolute() else (root / p)


SOC_PROJECT_SCHEMA = "faultflow_project_v2"


@dataclass(frozen=True)
class SocSpec:
    """The SoC: its glue's top module and RTL, and the SoC inputs every test holds
    (``hold``, beside the wrapper modes' own)."""

    top: str
    rtl: Path
    hold: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class SocProject:
    """A ``faultflow_project_v2`` project: blocks wrapped with the IEEE 1500 wrapper
    ff.py wrap puts on, each scanned on its own, and the SoC their glue composes."""

    name: str
    base_config: Path
    blocks: tuple[BlockSpec, ...]
    soc: SocSpec
    root: Path = field(default=Path("."))


def load_soc_project(path: str | Path) -> SocProject:
    """Parse and check a ``faultflow_project_v2`` manifest; paths resolve against
    its directory.

    ```json
    { "schema": "faultflow_project_v2", "name": "soc2", "base_config": "config.ofs",
      "blocks": [ {"name": "blkA", "top": "alu", "soc_instance": "u_a",
                   "generic_json": "blkA/alu_scan.json",
                   "scan_manifest": "blkA/scan_manifest.json"} ],
      "soc": {"top": "soc_top", "rtl": "soc_glue.v", "hold": {"test_en": 1}} }
    ```

    ``generic_json`` and ``scan_manifest`` are what ``ff.py scan`` wrote for the
    block with ``[wrap] enabled``; ``soc_instance`` is its instance in the glue."""
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
    if schema == _V1_SCHEMA:
        raise ProjectError(
            f"{_V1_SCHEMA} describes the abstract $wbc_* wrapper; wrap each "
            "block with [wrap] enabled and ff.py scan, then write a "
            f"{SOC_PROJECT_SCHEMA} manifest: blocks with soc_instance, generic_json "
            "and scan_manifest, and soc with top and rtl"
        )
    if schema != SOC_PROJECT_SCHEMA:
        raise ProjectError(
            f"unsupported project schema {schema!r}; expected {SOC_PROJECT_SCHEMA!r}"
        )
    root = manifest_path.resolve().parent
    raw_blocks = _req(data, "blocks", "project")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ProjectError("project.blocks must be a non-empty list")
    blocks: list[BlockSpec] = []
    names: set[str] = set()
    instances: set[str] = set()
    for i, raw in enumerate(raw_blocks):
        where = f"project.blocks[{i}]"
        if not isinstance(raw, dict):
            raise ProjectError(f"{where}: must be an object")
        top = str(_req(raw, "top", where))
        name = str(raw.get("name", top))
        instance = str(_req(raw, "soc_instance", where))
        if name in names:
            raise ProjectError(f"{where}: duplicate block name {name!r}")
        if instance in instances:
            raise ProjectError(f"{where}: duplicate soc_instance {instance!r}")
        names.add(name)
        instances.add(instance)
        blocks.append(
            BlockSpec(
                name=name,
                top=top,
                generic_json=_resolve(root, _req(raw, "generic_json", where), where),
                scan_manifest=_resolve(root, _req(raw, "scan_manifest", where), where),
                soc_instance=instance,
            )
        )
    raw_soc = _req(data, "soc", "project")
    if not isinstance(raw_soc, dict):
        raise ProjectError("project.soc must be an object")
    soc_top = str(_req(raw_soc, "top", "project.soc"))
    if soc_top in {block.top for block in blocks}:
        raise ProjectError(f"soc.top {soc_top!r} is a block's top")
    raw_hold = raw_soc.get("hold", {})
    if not isinstance(raw_hold, dict) or not all(
        value in (0, 1) for value in raw_hold.values()
    ):
        raise ProjectError("soc.hold must map SoC inputs to 0 or 1")
    return SocProject(
        name=str(data.get("name", manifest_path.stem)),
        base_config=_resolve(root, data.get("base_config", "config.ofs"), "project"),
        blocks=tuple(blocks),
        soc=SocSpec(
            top=soc_top,
            rtl=_resolve(root, _req(raw_soc, "rtl", "project.soc"), "project.soc"),
            hold=tuple(sorted((str(k), int(v)) for k, v in raw_hold.items())),
        ),
        root=root,
    )
