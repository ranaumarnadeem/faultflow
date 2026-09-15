"""Tests for check_compaction_structure, using hand-built fixtures shaped
like real Yosys/abc output (see compaction_checks.py's module docstring and
compression_checks.py's, which documents the empty-port_directions finding
this mirrors): Sky130 cell types with an EMPTY `port_directions` dict, and a
single-chain fanout row optimized to a pure net alias (no gate at all)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.scan.compaction_checks import check_compaction_structure


def _xor2_cell(a: int, b: int, y: int, name: str = "u_xor") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__xor2_1",
            "port_directions": {},
            "connections": {"A": [a], "B": [b], "X": [y]},
        }
    }


def _inv_cell(a: int, y: int, name: str = "u_inv") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__inv_1",
            "port_directions": {},
            "connections": {"A": [a], "Y": [y]},
        }
    }


def _mux2_cell(a0: int, a1: int, s: int, y: int, name: str = "u_mux") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__mux2_1",
            "port_directions": {},
            "connections": {"A0": [a0], "A1": [a1], "S": [s], "X": [y]},
        }
    }


def _manifest(tmp_path: Path, module: dict, compaction: dict) -> dict:
    composed_json = tmp_path / "composed.json"
    composed_json.write_text(
        json.dumps({"modules": {"core_top_compacted": module}}), encoding="utf-8"
    )
    compaction = dict(compaction)
    if compaction.get("enabled"):
        compaction.setdefault("composed_json", str(composed_json))
        compaction.setdefault("composed_top", "core_top_compacted")
    return {
        "top": "core_top",
        "compaction": compaction,
    }


@pytest.mark.unit
def test_check_compaction_structure_noop_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"ports": {}, "cells": {}, "netnames": {}},
        {"enabled": False},
    )
    result = check_compaction_structure(manifest)
    assert result.passed
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.unit
def test_check_compaction_structure_accepts_alias_and_xor_taps(tmp_path: Path) -> None:
    # scan_out_0/scan_out_1 = bits [1, 2]. tdo[0]'s fanout is [0] -- Yosys
    # optimizes a single-tap XOR to a pure net alias (net 1 IS tdo[0], no gate
    # at all). tdo[1]'s fanout is [0, 1] -- a real XOR2 gate combines bits 1
    # and 2 into a new net (3) that becomes tdo[1].
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "scan_out_1": {"bits": [2]},
            "tdo": {"bits": [1, 3]},
        },
        "cells": _xor2_cell(a=1, b=2, y=3),
    }
    compaction = {
        "enabled": True,
        "num_outputs": 2,
        "scan_out_ports": ["scan_out_0", "scan_out_1"],
        "channel_port": "tdo",
        "fanout": [[0], [0, 1]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    result = check_compaction_structure(manifest)

    assert result.passed, result.errors


@pytest.mark.unit
def test_check_compaction_structure_detects_mismatched_fanout(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "scan_out_1": {"bits": [2]},
            "tdo": {"bits": [3]},
        },
        "cells": _xor2_cell(a=1, b=2, y=3),
    }
    compaction = {
        "enabled": True,
        "num_outputs": 1,
        "scan_out_ports": ["scan_out_0", "scan_out_1"],
        "channel_port": "tdo",
        # Manifest claims tdo[0] = scan_out_0 only, but the netlist actually
        # XORs both chains together.
        "fanout": [[0]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    result = check_compaction_structure(manifest)

    assert not result.passed
    assert any("does not match" in err for err in result.errors)


@pytest.mark.unit
def test_check_compaction_structure_rejects_non_linear_gate_in_cone(
    tmp_path: Path,
) -> None:
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "scan_out_1": {"bits": [2]},
            "tdo": {"bits": [3]},
        },
        # A mux (non-linear) sits in tdo[0]'s cone instead of an XOR.
        "cells": _mux2_cell(a0=1, a1=2, s=1, y=3),
    }
    compaction = {
        "enabled": True,
        "num_outputs": 1,
        "scan_out_ports": ["scan_out_0", "scan_out_1"],
        "channel_port": "tdo",
        "fanout": [[0, 1]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    result = check_compaction_structure(manifest)

    assert not result.passed
    assert any("non-linear gate" in err for err in result.errors)


@pytest.mark.unit
def test_check_compaction_structure_rejects_inverted_cone(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "tdo": {"bits": [2]},
        },
        "cells": _inv_cell(a=1, y=2),
    }
    compaction = {
        "enabled": True,
        "num_outputs": 1,
        "scan_out_ports": ["scan_out_0"],
        "channel_port": "tdo",
        "fanout": [[0]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    result = check_compaction_structure(manifest)

    assert not result.passed
    assert any("inverted" in err for err in result.errors)


@pytest.mark.unit
def test_check_compaction_structure_rejects_undeclared_leaf(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "tdo": {"bits": [3]},
        },
        # net 3 is driven by an XOR of net 1 and net 99 -- net 99 is not a
        # declared scan-out leaf and has no driver of its own.
        "cells": _xor2_cell(a=1, b=99, y=3),
    }
    compaction = {
        "enabled": True,
        "num_outputs": 1,
        "scan_out_ports": ["scan_out_0"],
        "channel_port": "tdo",
        "fanout": [[0, 1]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    result = check_compaction_structure(manifest)

    assert not result.passed
    assert any("no driver" in err for err in result.errors)
