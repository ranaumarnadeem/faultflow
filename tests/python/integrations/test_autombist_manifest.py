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


def _test_access() -> dict[str, Any]:
    """A `test_access` block as `autombist wrap-test-access` records it: the
    memory's entry names no sources of its own here."""
    return {
        "wrapped": True,
        "top_module": "top",
        "output_verilog": "test-access/top_test_access.v",
        "output_dir": "/written/by/autombist/never/read",
        "icl_path": "/abs/top_test_access.icl",
        "boundary_ports": ["tck", "tms", "tdi", "tdo", "trst_n"],
        "memory_blackboxed": True,
        "instances": [
            {
                "category": "memory",
                "hierarchical_path": "u_mem",
                "hierarchy_hint": "blackbox",
                "instance_name": "u_mem",
                "module_type": "mem",
            },
            {
                "category": "mbist_controller",
                "hierarchical_path": "u_algo",
                "hierarchy_hint": "separate",
                "instance_name": "u_algo",
                "module_type": "\\$paramod$abc\\algo_top",
            },
            {
                "category": "ijtag_sib",
                "hierarchical_path": "warptap_sib_go",
                "hierarchy_hint": "separate",
                "instance_name": "warptap_sib_go",
                "module_type": "sib_cell",
                "instrument": "go",
                "sib_name": "sib_go",
            },
            {
                "bit": 0,
                "category": "ijtag_tdr",
                "hierarchical_path": "warptap_sib_go_inst_0",
                "hierarchy_hint": "separate",
                "instance_name": "warptap_sib_go_inst_0",
                "module_type": "instrument_write",
                "sib_name": "sib_go",
            },
            {
                "category": "jtag_tap",
                "hierarchical_path": "warptap_tap_core",
                "hierarchy_hint": "separate",
                "instance_name": "warptap_tap_core",
                "module_type": "tap_core",
            },
        ],
        "instruments": [
            {
                "name": "go",
                "role": "control",
                "width": 1,
                "sib": "warptap_sib_go",
                "tdr_bits": ["warptap_sib_go_inst_0"],
            }
        ],
    }


def _wrapped(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    data = _base_manifest(tmp_path)
    data["test_access"] = {**_test_access(), **changes}
    return data


def test_test_access_block_is_parsed(tmp_path: Path) -> None:
    manifest = load_autombist_manifest(_write(tmp_path, _wrapped(tmp_path)))

    access = manifest.test_access
    assert access is not None
    assert access.top_module == "top"
    # Relative paths resolve against the manifest's directory, absolute ones
    # stay as written.
    assert access.output_verilog == tmp_path / "test-access/top_test_access.v"
    assert access.icl_path == Path("/abs/top_test_access.icl")
    assert access.boundary_ports == ("tck", "tms", "tdi", "tdo", "trst_n")
    mem, algo, sib, tdr, tap = access.instances
    # The memory's stub is the base instance's.
    assert mem.sources == (tmp_path / "mem_bbox.v",)
    assert algo.module_type == "\\$paramod$abc\\algo_top"
    assert algo.sources == ()
    assert (sib.category, sib.sib_name, sib.instrument) == ("ijtag_sib", "sib_go", "go")
    assert (tdr.sib_name, tdr.bit) == ("sib_go", 0)
    assert tap.category == "jtag_tap"
    (go,) = access.instruments
    assert (go.name, go.role, go.width) == ("go", "control", 1)
    assert (go.sib, go.tdr_bits) == ("warptap_sib_go", ("warptap_sib_go_inst_0",))


def test_a_block_without_the_newer_fields_selects_the_network_with_extest(
    tmp_path: Path,
) -> None:
    """A block written before the network's instruction was recorded means EXTEST
    (opcode 0), control TDRs cleared by TRST only and status captured directly."""
    access = load_autombist_manifest(_write(tmp_path, _wrapped(tmp_path))).test_access
    assert access is not None
    assert (access.network_instruction, access.network_opcode) == ("EXTEST", 0)
    assert (access.chip_reset, access.chip_reset_active_low) == (None, True)
    assert access.idcode_value is None and access.bsdl_path is None
    assert [i.capture_sync for i in access.instruments] == [False]


def test_the_network_instruction_and_the_chip_reset_are_parsed(tmp_path: Path) -> None:
    data = _wrapped(
        tmp_path,
        network_instruction="IJTAG_ACCESS",
        network_opcode=12,
        chip_reset={"port": "rst", "active": "high"},
        idcode_value=0x5CA1AB1F,
        bsdl_path="top.bsd",
    )
    data["test_access"]["instruments"][0]["capture_sync"] = False
    access = load_autombist_manifest(_write(tmp_path, data)).test_access
    assert access is not None
    assert (access.network_instruction, access.network_opcode) == ("IJTAG_ACCESS", 12)
    assert (access.chip_reset, access.chip_reset_active_low) == ("rst", False)
    assert access.idcode_value == 0x5CA1AB1F
    assert access.bsdl_path == tmp_path / "top.bsd"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"network_instruction": "IJTAG_ACCESS"}, "network_opcode must be the opcode"),
        ({"network_opcode": -1}, "network_opcode"),
        ({"network_opcode": True}, "network_opcode"),
        ({"network_instruction": ""}, "network_instruction must be a name"),
        ({"chip_reset": "rst"}, "chip_reset must be"),
        ({"chip_reset": {"port": "rst", "active": "lo"}}, "'low' or 'high'"),
        ({"idcode_value": "0x1"}, "idcode_value must be an integer"),
    ],
)
def test_a_bad_network_field_is_rejected(
    tmp_path: Path, changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(AutombistManifestError, match=match):
        load_autombist_manifest(_write(tmp_path, _wrapped(tmp_path, **changes)))


def test_capture_sync_is_true_or_false(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instruments"][0]["capture_sync"] = "yes"
    with pytest.raises(AutombistManifestError, match="capture_sync"):
        load_autombist_manifest(_write(tmp_path, data))


def test_a_memory_entry_may_name_its_own_stub(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instances"][0]["sources"] = ["other_bbox.v"]
    manifest = load_autombist_manifest(_write(tmp_path, data))
    assert manifest.test_access is not None
    assert manifest.test_access.instances[0].sources == (tmp_path / "other_bbox.v",)


def test_an_unwrapped_test_access_block_is_no_test_access(tmp_path: Path) -> None:
    manifest = load_autombist_manifest(
        _write(tmp_path, _wrapped(tmp_path, wrapped=False))
    )
    assert manifest.test_access is None


def test_a_memory_built_from_its_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AutombistManifestError, match="memory_blackboxed"):
        load_autombist_manifest(
            _write(tmp_path, _wrapped(tmp_path, memory_blackboxed=False))
        )


def test_a_memory_without_a_stub_is_rejected(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instances"][0]["hierarchical_path"] = "u_other_mem"
    with pytest.raises(AutombistManifestError, match="has no stub"):
        load_autombist_manifest(_write(tmp_path, data))


def test_duplicate_test_access_instance_rejected(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instances"][4]["hierarchical_path"] = "u_algo"
    with pytest.raises(AutombistManifestError, match="duplicate hierarchical_path"):
        load_autombist_manifest(_write(tmp_path, data))


def test_an_instrument_must_name_listed_instances(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instruments"][0]["tdr_bits"] = ["warptap_sib_gone_inst_0"]
    with pytest.raises(AutombistManifestError, match="warptap_sib_gone_inst_0"):
        load_autombist_manifest(_write(tmp_path, data))


def test_an_instrument_needs_one_tdr_cell_per_bit(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instruments"][0]["width"] = 2
    with pytest.raises(AutombistManifestError, match="one TDR cell per bit"):
        load_autombist_manifest(_write(tmp_path, data))


def test_an_instrument_role_is_control_or_status(tmp_path: Path) -> None:
    data = _wrapped(tmp_path)
    data["test_access"]["instruments"][0]["role"] = "both"
    with pytest.raises(AutombistManifestError, match="role"):
        load_autombist_manifest(_write(tmp_path, data))
