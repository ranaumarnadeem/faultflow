"""Tests for check_compression_structure, using hand-built fixtures shaped
exactly like real Yosys/abc output (confirmed by direct inspection of a real
insert_compression run -- see compression_checks.py's module docstring):
Sky130 cell types with an EMPTY `port_directions` dict (confirmed true for
every real Sky130 library cell in this project's synthesized JSON -- only
the synthetic $scanff_faultflow splice cells populate it), and a single-tap
chain optimized to a pure net alias (no gate at all)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.scan.compression_checks import check_compression_structure


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


def _manifest(tmp_path: Path, module: dict, compression: dict) -> dict:
    composed_json = tmp_path / "composed.json"
    composed_json.write_text(
        json.dumps({"modules": {"core_top_compressed": module}}), encoding="utf-8"
    )
    compression = dict(compression)
    if compression.get("enabled"):
        compression.setdefault("composed_json", str(composed_json))
        compression.setdefault("composed_top", "core_top_compressed")
    return {
        # The manifest's own "top" is the PRE-compression design name --
        # check_compression_structure never reads generic_json/this file at
        # all, only compression["composed_json"]/["composed_top"] (see the
        # compression CLI-wiring plan's "required fix" section).
        "top": "core_top",
        "compression": compression,
    }


@pytest.mark.unit
def test_check_compression_structure_noop_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"ports": {}, "cells": {}, "netnames": {}},
        {"enabled": False},
    )
    result = check_compression_structure(manifest)
    assert result.passed
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.unit
def test_check_compression_structure_accepts_alias_and_xor_taps(
    tmp_path: Path,
) -> None:
    # tap_source_net "effective_state" = bits [1, 2] (2 channels). chain0's tap
    # is [0] -- Yosys optimizes a single-tap phase-shifter assign to a pure
    # net alias (net 1 IS scan_in_0, no gate at all) -- confirmed empirically.
    # chain1's tap is [0, 1] -- a real XOR2 gate combines bits 1 and 2 into a
    # new net (3) that becomes scan_in_1.
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1, 2]},
            "scan_in_0": {"bits": [1]},
            "scan_in_1": {"bits": [3]},
        },
        "cells": _xor2_cell(a=1, b=2, y=3),
    }
    compression = {
        "enabled": True,
        "num_channels": 2,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0", "scan_in_1"],
        "phase_shifter_taps": [[0], [0, 1]],
    }
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert result.passed, result.errors


@pytest.mark.unit
def test_check_compression_structure_detects_mismatched_taps(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1, 2]},
            "scan_in_0": {"bits": [3]},
        },
        "cells": _xor2_cell(a=1, b=2, y=3),
    }
    compression = {
        "enabled": True,
        "num_channels": 2,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0"],
        # Manifest claims scan_in_0 = bit 0 only, but the netlist actually
        # XORs bits 0 and 1 together.
        "phase_shifter_taps": [[0]],
    }
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("do not match" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_non_linear_gate_in_cone(
    tmp_path: Path,
) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1, 2]},
            "scan_in_0": {"bits": [3]},
        },
        # A mux (non-linear) sits in scan_in_0's cone instead of an XOR --
        # this must be flagged, not silently accepted as "close enough".
        "cells": _mux2_cell(a0=1, a1=2, s=1, y=3),
    }
    compression = {
        "enabled": True,
        "num_channels": 2,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0"],
        "phase_shifter_taps": [[0, 1]],
    }
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("non-linear gate" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_inverted_cone(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1]},
            "scan_in_0": {"bits": [2]},
        },
        "cells": _inv_cell(a=1, y=2),
    }
    compression = {
        "enabled": True,
        "num_channels": 1,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0"],
        "phase_shifter_taps": [[0]],
    }
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("inverted" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_undeclared_leaf(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1, 2]},
            "scan_in_0": {"bits": [3]},
        },
        # net 3 is driven by an XOR of net 1 and net 99 -- net 99 is not a
        # tap_source_net bit and has no driver of its own -- undeclared leaf.
        "cells": _xor2_cell(a=1, b=99, y=3),
    }
    compression = {
        "enabled": True,
        "num_channels": 2,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0"],
        "phase_shifter_taps": [[0, 1]],
    }
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("no driver" in err for err in result.errors)
