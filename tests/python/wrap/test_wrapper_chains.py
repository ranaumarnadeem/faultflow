"""Scan insertion on a block wrapped in the IEEE 1500 wrapper (faultflow.wrap): the
wrapper's flops go on chains of their own, after the core's and in ring order; the
scan manifest records the wrapper; and a scan test holds its mode pins as its mode
says."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import ScanError, plan_scan_json, stitch_scan_json
from faultflow.wrap.block import wrap_block
from faultflow.wrap.errors import WrapError
from faultflow.wrap.record import VERSION, mode_holds, wrapper_record

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
TOP = "blk"


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}",
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in ("Q", "X") else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _block() -> dict[str, Any]:
    """clk 2, a 3, b 4, c 5: r0 = a & b, r1 = r0 ^ c; y = r1, z = r0 | c."""
    cells = {
        "g0": _cell("and2_1", A=3, B=4, X=10),
        "r0": _cell("dfxtp_1", CLK=2, D=10, Q=11),
        "g1": _cell("xor2_1", A=11, B=5, X=12),
        "r1": _cell("dfxtp_1", CLK=2, D=12, Q=13),
        "g2": _cell("or2_1", A=11, B=5, X=14),
    }
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in (("clk", 2), ("a", 3))},
        **{n: {"direction": "input", "bits": [b]} for n, b in (("b", 4), ("c", 5))},
        "y": {"direction": "output", "bits": [13]},
        "z": {"direction": "output", "bits": [14]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


def _wrapped(tmp_path: Path, block: dict[str, Any] | None = None) -> Path:
    wrapped = tmp_path / "wrapped.json"
    netlist = wrap_block(block or _block(), TOP, CELL_MAP).netlist
    wrapped.write_text(json.dumps(netlist), encoding="utf-8")
    return wrapped


def _chains(result: Any) -> list[tuple[str, str, list[str]]]:
    return [
        (chain.kind, chain.scan_in, [cell.instance for cell in chain.cells])
        for chain in result.chains
    ]


def test_the_wrapper_flops_get_chains_no_longer_than_the_core_chains(
    tmp_path: Path,
) -> None:
    """Two core flops on one chain, so the five wrapper flops get three chains of at
    most two, in ring order: a, b | c, y | z."""
    result = stitch_scan_json(
        _wrapped(tmp_path), CELL_MAP_PATH, TOP, tmp_path / "scan.json"
    )
    assert _chains(result) == [
        ("core", "scan_in", ["r0", "r1"]),
        ("wrapper", "wbr_si_0", ["__wbr_i_a_ff", "__wbr_i_b_ff"]),
        ("wrapper", "wbr_si_1", ["__wbr_i_c_ff", "__wbr_o_y_ff"]),
        ("wrapper", "wbr_si_2", ["__wbr_o_z_ff"]),
    ]
    assert [chain.index for chain in result.chains] == [0, 1, 2, 3]
    assert result.scan_outputs == ["scan_out", "wbr_so_0", "wbr_so_1", "wbr_so_2"]
    scanned = json.loads((tmp_path / "scan.json").read_text(encoding="utf-8"))
    module = scanned["modules"][TOP]
    flop = module["cells"]["__wbr_i_a_ff"]
    assert flop["type"] == "\\$scanff_faultflow"
    assert flop["attributes"]["faultflow_wrapper_role"] == "ff"
    assert flop["connections"]["SE"] == module["ports"]["scan_en"]["bits"]


def test_wrapper_chains_sets_their_count(tmp_path: Path) -> None:
    wrapped = _wrapped(tmp_path)
    one = stitch_scan_json(
        wrapped, CELL_MAP_PATH, TOP, tmp_path / "one.json", wrapper_chains=1
    )
    assert _chains(one)[1] == (
        "wrapper",
        "wbr_si",
        [
            "__wbr_i_a_ff",
            "__wbr_i_b_ff",
            "__wbr_i_c_ff",
            "__wbr_o_y_ff",
            "__wbr_o_z_ff",
        ],
    )
    two_core = stitch_scan_json(
        wrapped, CELL_MAP_PATH, TOP, tmp_path / "two.json", scan_chains=2
    )
    assert [c.length for c in two_core.chains] == [1, 1, 1, 1, 1, 1, 1]
    with pytest.raises(ScanError, match="wrapper_chains must be between 1 and 5"):
        stitch_scan_json(
            wrapped, CELL_MAP_PATH, TOP, tmp_path / "six.json", wrapper_chains=6
        )


def test_a_block_with_no_flops_of_its_own_is_one_wrapper_chain(
    tmp_path: Path,
) -> None:
    block = _block()
    module = block["modules"][TOP]
    for flop in ("r0", "r1"):
        del module["cells"][flop]
    del module["ports"]["clk"]
    netlist = wrap_block(block, TOP, CELL_MAP, _options("wrck")).netlist
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps(netlist), encoding="utf-8")
    result = stitch_scan_json(wrapped, CELL_MAP_PATH, TOP, tmp_path / "scan.json")
    assert [(c.kind, c.scan_in, c.length) for c in result.chains] == [
        ("wrapper", "wbr_si", 5)
    ]
    assert result.scan_enable == "scan_en"


def _options(clock: str) -> Any:
    from faultflow.wrap.block import WrapOptions

    return WrapOptions(clock=clock)


def test_the_dry_run_plan_shows_the_wrapper_chains(tmp_path: Path) -> None:
    from faultflow.scan.reports import format_dry_run

    plan = plan_scan_json(_wrapped(tmp_path), CELL_MAP_PATH, TOP, wrapper_chains=1)
    text = format_dry_run(plan)
    assert "chain 1 (wrapper) length=5: wbr_si -> __wbr_i_a_ff" in text


def test_the_manifest_records_the_wrapper_and_the_holds_of_each_mode(
    tmp_path: Path,
) -> None:
    from faultflow.scan.reports import manifest_from_result

    result = stitch_scan_json(
        _wrapped(tmp_path), CELL_MAP_PATH, TOP, tmp_path / "scan.json", wrapper_chains=1
    )
    manifest: dict[str, Any] = manifest_from_result(
        result, tmp_path / "wrapped.json", tmp_path / "map.v", None
    )
    module = json.loads((tmp_path / "scan.json").read_text(encoding="utf-8"))
    record = wrapper_record(module["modules"][TOP], manifest["chains"])
    assert record is not None
    assert record["version"] == VERSION
    assert record["control"] == "pins"
    assert record["clock"] == {"port": "clk", "net": 2}
    by_label = {cell["label"]: cell for cell in record["cells"]}
    assert [cell["label"] for cell in record["cells"]] == ["a", "b", "c", "y", "z"]
    assert (by_label["a"]["sys_net"], by_label["a"]["chain"]) == (3, 1)
    assert by_label["y"]["core_net"] == 13
    assert by_label["z"]["position"] == 4
    assert by_label["a"]["ff"] == "__wbr_i_a_ff"
    manifest["wrapper"] = record
    assert mode_holds(manifest, "functional") == {"wbr_intest": 0, "wbr_extest": 0}
    assert mode_holds(manifest, "intest") == {"wbr_intest": 1, "wbr_extest": 0}
    assert mode_holds(manifest, "extest") == {"wbr_intest": 0, "wbr_extest": 1}
    assert mode_holds({}, "intest") == {}
    with pytest.raises(WrapError, match="no wrapper mode"):
        mode_holds(manifest, "bist")
    assert wrapper_record(_block()["modules"][TOP], []) is None


def _manifest_validator() -> Any:
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")  # jsonschema 4.18 on
    schemas = [
        json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
        for name in ("scan_manifest.schema.json", "wrapper.schema.json")
    ]
    registry = referencing.Registry().with_resources(
        (schema["$id"], referencing.Resource.from_contents(schema))
        for schema in schemas
    )
    return jsonschema.Draft202012Validator(schemas[0], registry=registry)


def test_the_scan_manifest_schema_takes_a_plain_and_a_wrapped_manifest(
    tmp_path: Path,
) -> None:
    from faultflow.scan.reports import manifest_from_result

    validator = _manifest_validator()
    plain_source = tmp_path / "plain.json"
    plain_source.write_text(json.dumps(_block()), encoding="utf-8")
    plain = stitch_scan_json(plain_source, CELL_MAP_PATH, TOP, tmp_path / "p.json")
    validator.validate(
        manifest_from_result(plain, plain_source, tmp_path / "map.v", None)
    )
    wrapped = stitch_scan_json(
        _wrapped(tmp_path), CELL_MAP_PATH, TOP, tmp_path / "w.json"
    )
    manifest: dict[str, Any] = manifest_from_result(
        wrapped, tmp_path / "wrapped.json", tmp_path / "map.v", None
    )
    module = json.loads((tmp_path / "w.json").read_text(encoding="utf-8"))
    manifest["wrapper"] = wrapper_record(module["modules"][TOP], manifest["chains"])
    validator.validate(manifest)
    wrapper = manifest["wrapper"]
    wrapper["cells"][0]["side"] = "both"
    assert not validator.is_valid(manifest)


def _ofs(tmp_path: Path) -> tuple[Path, str]:
    """A config scanning the block with [wrap] enabled, and its text."""
    netlist = tmp_path / "blk.json"
    netlist.write_text(json.dumps(_block()), encoding="utf-8")
    ofs = tmp_path / "blk.ofs"
    base = (
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"output_root = {tmp_path / 'out'}\n\n[wrap]\nenabled = true\n\n"
    )
    ofs.write_text(base, encoding="utf-8")
    return ofs, base


def test_a_dry_run_scan_of_a_wrapped_block_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], require_cpp_core: None
) -> None:
    """ff.py scan --dry-run plans the wrapper chains of the wrapped block, and
    writes neither the wrapped netlist nor its report."""
    from faultflow.cli import main

    ofs, _ = _ofs(tmp_path)
    common = ["--top", TOP, "-c", str(ofs)]
    assert main(["init", *common]) == 0
    capsys.readouterr()
    assert main(["scan", "--dry-run", *common]) == 0
    plan = capsys.readouterr().out
    assert "chain 1 (wrapper) length=2: wbr_si_0 -> __wbr_i_a_ff" in plan
    assert "chain 3 (wrapper) length=1: wbr_si_2 -> __wbr_o_z_ff" in plan
    written = [p.name for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert not [name for name in written if "wrap" in name], written


def test_a_hold_against_the_mode_is_refused(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The functional scan test holds both mode pins at 0, so a [scan] hold of one at
    1 is refused (scan-check already); INTEST holds EXTEST at 0 and EXTEST holds
    INTEST at 0, so a hold of either at 1 is refused there. The manifest ff.py scan
    wrote is the schema's."""
    from faultflow.cli import main

    ofs, base = _ofs(tmp_path)
    common = ["--top", TOP, "-c", str(ofs)]
    ofs.write_text(base + "[scan]\nhold = wbr_intest:1\n", encoding="utf-8")
    assert main(["init", *common]) == 0
    assert main(["scan", *common]) == 0
    with pytest.raises(SystemExit):
        main(["scan-check", *common])
    ofs.write_text(base, encoding="utf-8")
    assert main(["scan-check", *common]) == 0
    ofs.write_text(base + "[scan]\nhold = wbr_intest:1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["extest", *common])
    ofs.write_text(base + "[scan]\nhold = wbr_extest:1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["intest", *common])
    manifests = list((tmp_path / "out").rglob("scan_manifest.json"))
    assert len(manifests) == 1
    written = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert written["wrapper"]["clock"]["port"] == "clk"
    _manifest_validator().validate(written)
