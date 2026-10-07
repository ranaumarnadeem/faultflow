"""The two scopes of a wrapped block's faults (faultflow.wrap.sides), and the INTEST
environment's harness (faultflow.wrap.environment), on a wrapped, scanned block."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import stitch_scan_json
from faultflow.scan.reports import manifest_from_result
from faultflow.wrap.block import WrapOptions, wrap_block
from faultflow.wrap.environment import (
    ENVIRONMENT,
    environment_ports,
    intest_harness,
)
from faultflow.wrap.errors import WrapError
from faultflow.wrap.record import wrapper_record
from faultflow.wrap.sides import EXTEST, INTEST, decoupled, owner

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
    """clk 2, a 3, d = {4, 5}; r0 = a & d[0], r1 = r0 ^ d[1]; y = r1, z = r0."""
    cells = {
        "g0": _cell("and2_1", A=3, B=4, X=10),
        "r0": _cell("dfxtp_1", CLK=2, D=10, Q=11),
        "g1": _cell("xor2_1", A=11, B=5, X=12),
        "r1": _cell("dfxtp_1", CLK=2, D=12, Q=13),
    }
    ports = {
        "clk": {"direction": "input", "bits": [2]},
        "a": {"direction": "input", "bits": [3]},
        "d": {"direction": "input", "bits": [4, 5]},
        "y": {"direction": "output", "bits": [13]},
        "z": {"direction": "output", "bits": [11]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


def _scanned(
    tmp_path: Path, exclude: tuple[str, ...] = ()
) -> tuple[Path, dict[str, Any]]:
    """The block wrapped and scanned, its wrapper on one chain (a, d[0], d[1], y,
    z), and its wrapper record."""
    wrapped = tmp_path / "wrapped.json"
    options = WrapOptions(exclude=exclude)
    wrapped.write_text(
        json.dumps(wrap_block(_block(), TOP, CELL_MAP, options).netlist),
        encoding="utf-8",
    )
    scanned = tmp_path / "scanned.json"
    result = stitch_scan_json(wrapped, CELL_MAP_PATH, TOP, scanned, wrapper_chains=1)
    manifest = manifest_from_result(result, wrapped, tmp_path / "map.v", None)
    module = json.loads(scanned.read_text(encoding="utf-8"))["modules"][TOP]
    chains: Any = manifest["chains"]
    record = wrapper_record(module, chains)
    assert record is not None
    return scanned, record


def _cells(wrapper: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(cell["label"]): cell for cell in wrapper["cells"]}


def test_the_harness_wires_every_wrapped_port_bit_to_the_environment(
    tmp_path: Path,
) -> None:
    scanned, wrapper = _scanned(tmp_path)
    netlist = json.loads(scanned.read_text(encoding="utf-8"))
    harness = intest_harness(netlist, TOP, wrapper)
    module = harness["modules"][TOP]
    original = netlist["modules"][TOP]
    assert set(module["ports"]) == set(original["ports"]) - {"a", "d", "y", "z"}
    environment = module["cells"][ENVIRONMENT]
    y, z = _cells(wrapper)["y"], _cells(wrapper)["z"]
    # An output cell drives its port bit through a net of its own.
    assert original["ports"]["y"]["bits"] == [y["sys_net"]]
    assert sorted(environment["connections"].values()) == sorted(
        [[3], [4], [5], [y["sys_net"]], [z["sys_net"]]]
    )
    drives = {
        bits[0]
        for pin, bits in environment["connections"].items()
        if environment["port_directions"][pin] == "output"
    }
    assert drives == {3, 4, 5}
    assert environment_ports(wrapper) == (["a", "d[0]", "d[1]"], ["y", "z"])
    assert ENVIRONMENT not in original["cells"]


def test_a_bus_wrapped_in_part_keeps_its_other_bits_as_one_bit_ports(
    tmp_path: Path,
) -> None:
    scanned, wrapper = _scanned(tmp_path, exclude=("d[1]",))
    netlist = json.loads(scanned.read_text(encoding="utf-8"))
    module = intest_harness(netlist, TOP, wrapper)["modules"][TOP]
    assert module["ports"]["d[1]"] == {"direction": "input", "bits": [5]}
    assert "d" not in module["ports"]
    y, z = _cells(wrapper)["y"], _cells(wrapper)["z"]
    environment = module["cells"][ENVIRONMENT]
    assert sorted(environment["connections"].values()) == sorted(
        [[3], [4], [y["sys_net"]], [z["sys_net"]]]
    )
    with pytest.raises(WrapError, match="already has a cell"):
        intest_harness(intest_harness(netlist, TOP, wrapper), TOP, wrapper)


def _rows(netlist: Path, blackboxes: list[str]) -> list[dict[str, Any]]:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    return [
        dict(row)
        for row in core.list_site_keys(
            str(netlist), str(CELL_MAP_PATH), "fail", blackboxes
        )
    ]


def test_the_harness_has_the_scanned_netlists_fault_sites(
    tmp_path: Path, require_cpp_core: None
) -> None:
    scanned, wrapper = _scanned(tmp_path)
    harness = tmp_path / "harness.json"
    netlist = json.loads(scanned.read_text(encoding="utf-8"))
    harness.write_text(json.dumps(intest_harness(netlist, TOP, wrapper)), "utf-8")
    keys = sorted(row["site_key"] for row in _rows(scanned, []))
    assert sorted(r["site_key"] for r in _rows(harness, [ENVIRONMENT])) == keys


def _q(scanned: Path, flop: str) -> int:
    module = json.loads(scanned.read_text(encoding="utf-8"))["modules"][TOP]
    (net,) = module["cells"][flop]["connections"]["Q"]
    return int(net)


def test_each_fault_belongs_to_the_mode_that_activates_it(
    tmp_path: Path, require_cpp_core: None
) -> None:
    scanned, wrapper = _scanned(tmp_path)
    rows = _rows(scanned, [])
    by_key = {row["site_key"]: row for row in rows}
    intest, extest = wrapper["intest"]["net"], wrapper["extest"]["net"]

    def stem(net: int) -> dict[str, Any]:
        return by_key[f"net:{net}:stem"]

    def branches(cell: str, pin: str) -> list[dict[str, Any]]:
        return [
            row
            for row in rows
            if row["kind"] == "branch"
            and row["consumer_instance"] == cell
            and row["input_pin"] == pin
        ]

    def branch(cell: str, pin: str) -> dict[str, Any]:
        (found,) = branches(cell, pin)
        return found

    a, y, z = _cells(wrapper)["a"], _cells(wrapper)["y"], _cells(wrapper)["z"]
    for fault_type in ("sa0", "sa1"):
        # The system side: EXTEST drives input ports and observes output ports.
        assert owner(stem(a["sys_net"]), fault_type, wrapper) == EXTEST
        assert owner(stem(y["sys_net"]), fault_type, wrapper) == EXTEST
        assert owner(branch(y["mux"], "A1"), fault_type, wrapper) == EXTEST
        assert owner(branch(y["gate"], "B"), fault_type, wrapper) == EXTEST
        # The rest of each cell, and the core.
        assert owner(stem(a["core_net"]), fault_type, wrapper) == INTEST
        assert owner(branch(a["mux"], "A1"), fault_type, wrapper) == INTEST
        assert owner(branch(a["gate"], "B"), fault_type, wrapper) == INTEST
        assert owner(branch(y["ff"], "D"), fault_type, wrapper) == INTEST
        assert owner(stem(_q(scanned, y["ff"])), fault_type, wrapper) == INTEST
        assert owner(stem(12), fault_type, wrapper) == INTEST
        # z's flop is last on the chain: its Q also reads the scan out port, which
        # is no cell, so its stem is its mux's input -- and the chain's, whose
        # unload INTEST tests.
        assert not branches(z["mux"], "A1")
        assert owner(stem(_q(scanned, z["ff"])), fault_type, wrapper) == INTEST
    # A mode net's stuck-at, by the mode that holds it at the other value.
    assert owner(stem(intest), "sa0", wrapper) == INTEST
    assert owner(stem(intest), "sa1", wrapper) == EXTEST
    assert owner(stem(extest), "sa1", wrapper) == INTEST
    assert owner(stem(extest), "sa0", wrapper) == EXTEST
    assert owner(branch(a["mux"], "S"), "sa0", wrapper) == INTEST
    assert owner(branch(y["gate"], "A_N"), "sa1", wrapper) == EXTEST

    every = {(row["site_key"], t) for row in rows for t in ("sa0", "sa1")}
    left_by_intest = decoupled(rows, wrapper, INTEST)
    left_by_extest = decoupled(rows, wrapper, EXTEST)
    assert left_by_intest | left_by_extest == every
    assert not left_by_intest & left_by_extest
    assert (f"net:{intest}:stem", "sa1") in left_by_intest
    with pytest.raises(ValueError, match="no wrapper test mode"):
        decoupled(rows, wrapper, "functional")
