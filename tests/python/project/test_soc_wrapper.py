"""A composed SoC's wrappers as one record (faultflow.project.soc_wrapper), and its
chains traced (faultflow.project.chip): two wrapped, scanned blocks, glue that
strings their wrapper chains into one and brings their mode pins to SoC inputs --
block B's EXTEST pin through an inverter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.assemble import compose_soc
from faultflow.project.chip import chip_manifest
from faultflow.project.manifest import ProjectError
from faultflow.project.soc_wrapper import soc_wrapper
from faultflow.scan import stitch_scan_json
from faultflow.scan.reports import manifest_from_result
from faultflow.wrap.block import wrap_block
from faultflow.wrap.record import wrapper_record

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
SCAN_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            p: "output" if p in ("Q", "X", "Y") else "input" for p in conns
        },
        "connections": {p: [n] for p, n in conns.items()},
    }


def _block(tmp_path: Path, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """blk: clk 2, a 3; r0 = a; y = ~r0. Wrapped and scanned: its scan JSON and
    its wrapper record."""
    cells = {
        "r0": _cell("dfxtp_1", CLK=2, D=3, Q=10),
        "g0": _cell("inv_1", A=10, Y=11),
    }
    ports = {
        "clk": {"direction": "input", "bits": [2]},
        "a": {"direction": "input", "bits": [3]},
        "y": {"direction": "output", "bits": [11]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    wrapped = tmp_path / f"{name}_wrapped.json"
    netlist = wrap_block({"modules": {"blk": module}}, "blk", CELL_MAP).netlist
    wrapped.write_text(json.dumps(netlist), encoding="utf-8")
    scanned = tmp_path / f"{name}_scan.json"
    result = stitch_scan_json(wrapped, CELL_MAP_PATH, "blk", scanned, wrapper_chains=1)
    manifest: dict[str, Any] = manifest_from_result(
        result, wrapped, tmp_path / "map.v", None
    )
    data = json.loads(scanned.read_text(encoding="utf-8"))
    record = wrapper_record(data["modules"]["blk"], manifest["chains"])
    assert record is not None
    return data, record


def _instance(conns: dict[str, int]) -> dict[str, Any]:
    outputs = {"y", "scan_out", "wbr_so"}
    return {
        "hide_name": 0,
        "type": "blk",
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _glue(b_extest: int) -> dict[str, Any]:
    """soc: clk 2, scan_en 3, si_a 4, si_b 5, wsi 6, t_intest 7, t_extest 8,
    t_extest_n 9, din 12; so_a 20, so_b 21, wso 22, dout 23. A's y drives B's a;
    B's EXTEST pin is net `b_extest`."""
    common = {"clk": 2, "scan_en": 3, "wbr_intest": 7}
    ports = {
        name: {"direction": "input", "bits": [net]}
        for name, net in (
            ("clk", 2),
            ("scan_en", 3),
            ("si_a", 4),
            ("si_b", 5),
            ("wsi", 6),
            ("t_intest", 7),
            ("t_extest", 8),
            ("t_extest_n", 9),
            ("din", 12),
        )
    }
    ports.update(
        {
            name: {"direction": "output", "bits": [net]}
            for name, net in (("so_a", 20), ("so_b", 21), ("wso", 22), ("dout", 23))
        }
    )
    cells = {
        "u_a": _instance(
            {
                **common,
                "wbr_extest": 8,
                "a": 12,
                "y": 30,
                "scan_in": 4,
                "scan_out": 20,
                "wbr_si": 6,
                "wbr_so": 31,
            }
        ),
        "u_b": _instance(
            {
                **common,
                "wbr_extest": b_extest,
                "a": 30,
                "y": 23,
                "scan_in": 5,
                "scan_out": 21,
                "wbr_si": 31,
                "wbr_so": 22,
            }
        ),
        "g_inv": _cell("inv_1", A=9, Y=32),
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {"soc": module}}


def _compose(
    tmp_path: Path, b_extest: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    block_a, record_a = _block(tmp_path, "a")
    block_b, record_b = _block(tmp_path, "b")
    remaps: dict[str, dict[int, int | str]] = {}
    composed = compose_soc(
        glue_json=_glue(b_extest),
        soc_top="soc",
        blocks={"u_a": block_a, "u_b": block_b},
        block_module={"u_a": "blk", "u_b": "blk"},
        block_names={"u_a": "A", "u_b": "B"},
        tag_blocks=True,
        remaps=remaps,
    )
    path = tmp_path / "soc.json"
    path.write_text(json.dumps(composed), encoding="utf-8")
    module = composed["modules"]["soc"]
    manifest = chip_manifest(module, "soc", SCAN_MAP, path)
    blocks = {"u_a": ("A", record_a), "u_b": ("B", record_b)}
    return module, manifest, blocks, remaps


def test_the_soc_chains_and_wrapper_record(tmp_path: Path) -> None:
    module, manifest, blocks, remaps = _compose(tmp_path, b_extest=32)
    assert [(c["scan_in"], c["kind"], c["length"]) for c in manifest["chains"]] == [
        ("si_a", "core", 1),
        ("si_b", "core", 1),
        ("wsi", "wrapper", 4),
    ]
    record = soc_wrapper(module, SCAN_MAP, blocks, remaps, manifest["chains"])
    # In EXTEST each block's pins are (0, 1): B's EXTEST pin through the inverter.
    assert record["holds"]["extest"] == {"t_intest": 0, "t_extest": 1, "t_extest_n": 0}
    assert record["holds"]["intest"] == {"t_intest": 1, "t_extest": 0, "t_extest_n": 1}
    assert record["holds"]["functional"]["t_extest_n"] == 1
    # The inverter's output and input are on B's EXTEST pin's path, held by
    # INTEST at 0 and (inverted) at 1.
    assert (record["mode_nets"]["32"], record["mode_nets"]["9"]) == (0, 1)
    labels = [cell["label"] for cell in record["cells"]]
    assert labels == ["A/a", "A/y", "B/a", "B/y"]
    by_label = {cell["label"]: cell for cell in record["cells"]}
    # A's y and B's a are one net in the SoC.
    assert by_label["A/y"]["sys_net"] == by_label["B/a"]["sys_net"]
    assert by_label["B/y"]["ff"] == "u_b__" + blocks["u_b"][1]["cells"][1]["ff"]
    assert [cell["position"] for cell in record["cells"]] == [0, 1, 2, 3]


def test_a_soc_input_two_blocks_need_apart_is_refused(tmp_path: Path) -> None:
    """B's EXTEST pin through an inverter from A's: in EXTEST A needs t_extest at 1
    and B at 0."""
    glue_conflict = tmp_path / "conflict"
    glue_conflict.mkdir()
    module, manifest, blocks, remaps = _compose(glue_conflict, b_extest=32)
    module["cells"]["g_inv"]["connections"]["A"] = [8]
    with pytest.raises(ProjectError, match="t_extest"):
        soc_wrapper(module, SCAN_MAP, blocks, remaps, manifest["chains"])


def test_each_soc_fault_site_is_the_block_faults_it_stands_for(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.project.identity import SOC, BlockSites, soc_identities, stem_key
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    module, manifest, blocks, remaps = _compose(tmp_path, b_extest=32)

    def rows(path: Path) -> tuple[Any, ...]:
        return tuple(
            dict(row)
            for row in core.list_site_keys(str(path), str(CELL_MAP_PATH), "fail", [])
        )

    sites = {}
    for instance, name in (("u_a", "a"), ("u_b", "b")):
        data = json.loads((tmp_path / f"{name}_scan.json").read_text("utf-8"))
        ports = data["modules"]["blk"]["ports"]
        inputs = frozenset(
            bit
            for port in ports.values()
            if port["direction"] == "input"
            for bit in port["bits"]
        )
        sites[instance] = BlockSites(
            blocks[instance][0],
            rows(tmp_path / f"{name}_scan.json"),
            inputs,
            remaps[instance],
        )
    identities = soc_identities(rows(tmp_path / "soc.json"), sites)
    by_label = {
        cell["label"]: cell
        for cell in soc_wrapper(module, SCAN_MAP, blocks, remaps, manifest["chains"])[
            "cells"
        ]
    }
    record_a, record_b = blocks["u_a"][1], blocks["u_b"][1]
    a_y = next(c for c in record_a["cells"] if c["label"] == "y")
    b_a = next(c for c in record_b["cells"] if c["label"] == "a")
    # The wire from A's y to B's a: one SoC site, each block's port stem.
    wire = by_label["A/y"]["sys_net"]
    assert sorted(identities[stem_key(wire)]) == [
        ("A", stem_key(a_y["sys_net"])),
        ("B", stem_key(b_a["sys_net"])),
    ]
    # The glue inverter's output is the glue's, and B's EXTEST pin's.
    assert (SOC, stem_key(32)) in identities[stem_key(32)]
    # Every block fault site of a block's core is one of the SoC's.
    a_core = {
        ("A", str(row["site_key"]))
        for row in sites["u_a"].rows
        if ":r0:" in str(row["site_key"]) or ":g0:" in str(row["site_key"])
    }
    found = {identity for ids in identities.values() for identity in ids}
    assert a_core and a_core <= found


def test_on_a_soc_the_glue_and_the_wires_between_blocks_are_extests(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Who owns a SoC's faults (faultflow.wrap.sides): the glue, the SoC's mode nets
    (whichever way stuck) and the wires the wrappers' system sides are on, EXTEST;
    a block's core, and a SoC input only one block's core reads, INTEST. A branch
    of a mode net into a block's cell is the block's, by its polarity."""
    import copy

    from faultflow.runner.runner import _load_core
    from faultflow.wrap.sides import EXTEST, FAULT_TYPES, INTEST, owner, sides_of

    core = _load_core()
    assert core is not None
    module, manifest, blocks, remaps = _compose(tmp_path, b_extest=32)
    record = soc_wrapper(module, SCAN_MAP, blocks, remaps, manifest["chains"])
    rows = [
        dict(row)
        for row in core.list_site_keys(
            str(tmp_path / "soc.json"), str(CELL_MAP_PATH), "fail", []
        )
    ]
    sides = sides_of(record, rows, module)
    assert sides.glue_cells == {"g_inv"}

    def owners(row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(owner(row, t, record, _sides=sides) for t in FAULT_TYPES)

    def stem(net: int | str) -> dict[str, Any]:
        return next(r for r in rows if r["kind"] == "stem" and r["yosys_net_id"] == net)

    by_label = {cell["label"]: cell for cell in record["cells"]}
    for net in (7, 8, 9, 32):  # the mode pins, and the inverter's output
        assert owners(stem(net)) == (EXTEST, EXTEST), net
    for net in (12, by_label["A/y"]["sys_net"], 3):  # din, A's y to B's a, scan_en
        assert owners(stem(net)) == (EXTEST, EXTEST), net
    assert owners(stem(remaps["u_a"][10])) == (INTEST, INTEST)  # A's r0
    assert owners(stem(4)) == (INTEST, INTEST)  # si_a: A's chain alone reads it
    # B's EXTEST pin, held at 0 by INTEST: stuck at 1 on its way into B is B's.
    into_b = [r for r in rows if r["kind"] == "branch" and r["yosys_net_id"] == 32]
    assert into_b and all(owners(r) == (EXTEST, INTEST) for r in into_b)
    untagged = copy.deepcopy(module)
    for cell in untagged["cells"].values():
        cell.get("attributes", {}).pop("faultflow_block", None)
    with pytest.raises(ValueError, match="name no block"):
        sides_of(record, rows, untagged)
