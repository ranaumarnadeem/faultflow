"""scan-check through gates the scan test keeps open: a chain whose head reads its
scan input through a mux a held select steers, and scan flops whose SE is the scan
enable ORed with a held 0 -- as an IEEE 1500 wrapper's serial-mode muxes and wrapper
scan enable are in parallel mode. Without the holds those aren't the manifest's
chains; with them they are, and an inverting path never is."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.checks import Transparent, check_scan_structure
from faultflow.scan.reports import hash_file

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads(
    (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
)
TOP = "paths"
SCAN = "$scanff_faultflow"
PORTS = {"clk": 2, "scan_in": 3, "scan_en": 4, "sel": 5, "test_x": 6, "d1": 7}


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    outputs = ("Q", "X", "Y")
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _manifest(tmp_path: Path, head: dict[str, Any]) -> dict[str, Any]:
    """ff0 -> ff1, scan_in -> scan_out; ff0's SDI is net 20, which `head` drives;
    both SEs are scan_en | test_x."""
    cells = {
        "head": head,
        "se_or": _cell("sky130_fd_sc_hd__or2_1", A=4, B=6, X=21),
        "ff0": _cell(SCAN, CLK=2, D=7, SDI=20, SE=21, Q=10),
        "ff1": _cell(SCAN, CLK=2, D=10, SDI=10, SE=21, Q=11),
    }
    ports: dict[str, Any] = {
        n: {"direction": "input", "bits": [b]} for n, b in PORTS.items()
    }
    ports["scan_out"] = {"direction": "output", "bits": [11]}
    generic = tmp_path / "paths_scan.json"
    generic.write_text(
        json.dumps(
            {
                "modules": {
                    TOP: {
                        "attributes": {"top": "1"},
                        "ports": ports,
                        "cells": cells,
                        "netnames": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "top": TOP,
        "generic_json": str(generic),
        "generic_json_hash": hash_file(generic),
        "scan_enable": "scan_en",
        "chains": [
            {
                "index": 0,
                "scan_in": "scan_in",
                "scan_out": "scan_out",
                "cells": ["ff0", "ff1"],
            }
        ],
    }


MUX_HEAD = _cell("sky130_fd_sc_hd__mux2_1", A0=3, A1=7, S=5, X=20)


@pytest.mark.unit
def test_paths_the_holds_keep_open_are_the_chains(
    tmp_path: Path, require_cpp_core: None
) -> None:
    manifest = _manifest(tmp_path, MUX_HEAD)
    strict = check_scan_structure(manifest)
    assert not strict.passed
    assert any("SE does not use shared scan enable" in e for e in strict.errors)
    held = Transparent(CELL_MAP, holds={"sel": 0, "test_x": 0})
    assert check_scan_structure(manifest, transparent=held).errors == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "holds, head, error",
    [
        # The mux steered to d1: the chain doesn't start at scan_in.
        ({"sel": 1, "test_x": 0}, MUX_HEAD, "does not match"),
        # test_x free: SE isn't scan enable alone.
        ({"sel": 0}, MUX_HEAD, "SE does not use shared scan enable"),
        # An inverter between scan_in and the chain: not the bits shifted in.
        (
            {"test_x": 0},
            _cell("sky130_fd_sc_hd__inv_1", A=3, Y=20),
            "does not match",
        ),
    ],
)
def test_a_path_not_held_open_or_inverting_is_refused(
    tmp_path: Path,
    require_cpp_core: None,
    holds: dict[str, int],
    head: dict[str, Any],
    error: str,
) -> None:
    result = check_scan_structure(
        _manifest(tmp_path, head), transparent=Transparent(CELL_MAP, holds=holds)
    )
    assert any(error in e for e in result.errors), result.errors


@pytest.mark.unit
def test_a_tied_flop_holds_a_path_open(tmp_path: Path, require_cpp_core: None) -> None:
    # sel is a programmed (or forced) non-scan flop's output at 0 in place of a
    # held input: scan-check knows its value from the non-scan setup.
    head = _cell("sky130_fd_sc_hd__mux2_1", A0=3, A1=7, S=30, X=20)
    manifest = _manifest(tmp_path, head)
    data = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    data["modules"][TOP]["cells"]["mode"] = _cell(
        "sky130_fd_sc_hd__dfxtp_1", CLK=5, D=7, Q=30
    )
    Path(manifest["generic_json"]).write_text(json.dumps(data), encoding="utf-8")
    manifest["generic_json_hash"] = hash_file(Path(manifest["generic_json"]))
    held = Transparent(CELL_MAP, holds={"test_x": 0}, known={30: 0})
    assert check_scan_structure(manifest, transparent=held).errors == []
    assert not check_scan_structure(
        manifest, transparent=Transparent(CELL_MAP, holds={"test_x": 0})
    ).passed
