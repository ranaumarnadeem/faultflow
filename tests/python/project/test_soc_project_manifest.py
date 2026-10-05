"""The faultflow_project_v2 manifest (faultflow.project.manifest.load_soc_project)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.manifest import ProjectError, load_soc_project


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "project.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _project() -> dict[str, Any]:
    return {
        "schema": "faultflow_project_v2",
        "name": "soc2",
        "blocks": [
            {
                "name": "blkA",
                "top": "alu",
                "soc_instance": "u_a",
                "generic_json": "a/alu_scan.json",
                "scan_manifest": "a/scan_manifest.json",
            }
        ],
        "soc": {"top": "soc_top", "rtl": "glue.v", "hold": {"test_en": 1}},
    }


def test_a_v2_project_names_its_blocks_and_its_soc(tmp_path: Path) -> None:
    project = load_soc_project(_write(tmp_path, _project()))
    (block,) = project.blocks
    assert (block.name, block.top, block.soc_instance) == ("blkA", "alu", "u_a")
    assert block.generic_json == tmp_path.resolve() / "a/alu_scan.json"
    assert (project.soc.top, project.soc.rtl.name) == ("soc_top", "glue.v")
    assert project.soc.hold == (("test_en", 1),)
    assert project.base_config == tmp_path.resolve() / "config.ofs"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "faultflow_project_v1"}, "abstract"),
        ({"schema": "nope"}, "unsupported project schema"),
        ({"soc": {"top": "alu", "rtl": "glue.v"}}, "is a block's top"),
        ({"soc": {"top": "soc_top", "rtl": "g.v", "hold": {"x": 2}}}, "0 or 1"),
        ({"soc": {"top": "soc_top"}}, "missing required field 'rtl'"),
    ],
)
def test_a_bad_v2_project_is_refused(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ProjectError, match=message):
        load_soc_project(_write(tmp_path, {**_project(), **change}))


def test_a_block_needs_its_soc_instance_once(tmp_path: Path) -> None:
    data = _project()
    del data["blocks"][0]["soc_instance"]
    with pytest.raises(ProjectError, match="soc_instance"):
        load_soc_project(_write(tmp_path, data))
    data = _project()
    data["blocks"].append({**data["blocks"][0], "name": "blkB"})
    with pytest.raises(ProjectError, match="duplicate soc_instance"):
        load_soc_project(_write(tmp_path, data))
