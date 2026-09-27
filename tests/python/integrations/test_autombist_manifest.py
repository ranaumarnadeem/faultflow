"""Tests for `faultflow.integrations.autombist.load_autombist_manifest`.

Mirrors `tests/python/project/test_manifest_scan_mode.py`'s exact style: a
`_base_manifest(root) -> dict` builder hand-writes tiny throwaway companion
files via `tmp_path`, a `_write(tmp_path, data) -> Path` helper JSON-dumps it,
then `load_autombist_manifest` is called directly and asserted against /
`pytest.raises(AutombistManifestError, match=...)`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.integrations.autombist import (
    AutombistManifestError,
    load_autombist_manifest,
)


def _base_manifest(root: Path) -> dict[str, Any]:
    (root / "wrapper.v").write_text("module top(); endmodule\n", encoding="utf-8")
    (root / "mem_bbox.v").write_text(
        "(* blackbox *) module mem(); endmodule\n", encoding="utf-8"
    )
    (root / "algo.sv").write_text("module algo(); endmodule\n", encoding="utf-8")
    return {
        "format": "autombist_instance_manifest",
        "schema_version": "1.0.0",
        "top_module": "top",
        "module_outdir": "/this/path/is/never/read",
        "sources": {"wrapper": "wrapper.v", "blackbox_stub": "mem_bbox.v"},
        "instances": [
            {
                "category": "memory",
                "hierarchical_path": "u_mem",
                "hierarchy_hint": "blackbox",
                "instance_name": "u_mem",
                "module_type": "mem",
                "sources": ["mem_bbox.v"],
                "geometry": {"addr_width": 4},
                "present_because": "always",
            },
            {
                "category": "mbist_controller",
                "hierarchical_path": "u_algo",
                "hierarchy_hint": "separate",
                "instance_name": "u_algo",
                "module_type": "algo_top",
                "sources": ["algo.sv"],
                "parameters": {"ADDR_WIDTH": 4},
                "present_because": "always",
            },
        ],
        "test_access": None,
    }


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_happy_path_parses_fields_and_resolves_sources(tmp_path: Path) -> None:
    manifest_path = _write(tmp_path, _base_manifest(tmp_path))
    manifest = load_autombist_manifest(manifest_path)

    assert manifest.top_module == "top"
    assert manifest.wrapper == tmp_path / "wrapper.v"
    assert manifest.root == tmp_path.resolve()
    assert manifest.test_access is None
    assert len(manifest.instances) == 2

    mem, algo = manifest.instances
    assert mem.hierarchy_hint == "blackbox"
    assert mem.sources == (tmp_path / "mem_bbox.v",)
    assert mem.geometry == {"addr_width": 4}
    assert algo.hierarchy_hint == "separate"
    assert algo.parameters == {"ADDR_WIDTH": 4}
    assert algo.sources == (tmp_path / "algo.sv",)


def test_module_outdir_is_never_used_for_path_resolution(tmp_path: Path) -> None:
    """Sources must resolve against the manifest FILE's own directory, not the
    JSON's own `module_outdir` string -- proven by deleting/mutating that
    field and asserting no effect on the result."""
    data = _base_manifest(tmp_path)
    del data["module_outdir"]
    manifest_path = _write(tmp_path, data)
    manifest = load_autombist_manifest(manifest_path)
    assert manifest.wrapper == tmp_path / "wrapper.v"

    data2 = _base_manifest(tmp_path)
    data2["module_outdir"] = "/completely/bogus/path/that/does/not/exist"
    manifest_path2 = _write(tmp_path, data2)
    manifest2 = load_autombist_manifest(manifest_path2)
    assert manifest2.wrapper == tmp_path / "wrapper.v"


def test_wrong_format_rejected(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    data["format"] = "something_else"
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="unsupported manifest format"):
        load_autombist_manifest(manifest_path)


def test_missing_format_rejected(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    del data["format"]
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="unsupported manifest format"):
        load_autombist_manifest(manifest_path)


@pytest.mark.parametrize("version", ["2.0.0", "0.9.0"])
def test_wrong_major_schema_version_rejected(tmp_path: Path, version: str) -> None:
    data = _base_manifest(tmp_path)
    data["schema_version"] = version
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="schema_version"):
        load_autombist_manifest(manifest_path)


def test_later_minor_schema_version_accepted(tmp_path: Path) -> None:
    """A same-major, later-minor schema_version from a newer autoMBIST release
    should still parse -- this is a third-party tool's schema, not one
    FaultFlow controls the versioning of."""
    data = _base_manifest(tmp_path)
    data["schema_version"] = "1.9.3"
    manifest_path = _write(tmp_path, data)
    manifest = load_autombist_manifest(manifest_path)
    assert manifest.top_module == "top"


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    del data["top_module"]
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="missing required field"):
        load_autombist_manifest(manifest_path)


def test_invalid_hierarchy_hint_rejected(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    data["instances"][0]["hierarchy_hint"] = "bogus"
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="hierarchy_hint"):
        load_autombist_manifest(manifest_path)


def test_duplicate_hierarchical_path_rejected(tmp_path: Path) -> None:
    data = _base_manifest(tmp_path)
    data["instances"][1]["hierarchical_path"] = data["instances"][0][
        "hierarchical_path"
    ]
    manifest_path = _write(tmp_path, data)
    with pytest.raises(AutombistManifestError, match="duplicate hierarchical_path"):
        load_autombist_manifest(manifest_path)


def test_invalid_json_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AutombistManifestError, match="not valid JSON"):
        load_autombist_manifest(manifest_path)


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(AutombistManifestError, match="not found"):
        load_autombist_manifest(tmp_path / "does_not_exist.json")


def test_non_dict_root_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(AutombistManifestError, match="must be a JSON object"):
        load_autombist_manifest(manifest_path)
