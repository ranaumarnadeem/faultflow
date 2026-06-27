"""Unit tests for _port_names and _expand_bus_bits helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.runner.runner import _expand_bus_bits, _port_names


def _write_json(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "netlist.json"
    p.write_text(json.dumps(data))
    return p


def _module(ports: dict, netnames: dict | None = None) -> dict:
    mod: dict = {"attributes": {"top": "1"}, "ports": ports, "cells": {}}
    if netnames is not None:
        mod["netnames"] = netnames
    return {"modules": {"top": mod}}


# ---------------------------------------------------------------------------
# _expand_bus_bits
# ---------------------------------------------------------------------------


def test_expand_bus_bits_with_indexed_netnames() -> None:
    netnames = {
        "o_result[0]": {"bits": [42]},
        "o_result[1]": {"bits": [43]},
        "o_result[2]": {"bits": [44]},
    }
    result = _expand_bus_bits("o_result", [42, 43, 44], netnames)
    assert set(result) == {"o_result[0]", "o_result[1]", "o_result[2]"}
    assert len(result) == 3


def test_expand_bus_bits_fallback_when_missing() -> None:
    # netnames only has the bus-level entry, not individual bits
    netnames = {"o_result": {"bits": [42, 43, 44]}}
    result = _expand_bus_bits("o_result", [42, 43, 44], netnames)
    assert result == ["o_result"]


def test_expand_bus_bits_fallback_partial() -> None:
    # Only some bits have indexed names — must fall back
    netnames = {
        "o_result[0]": {"bits": [42]},
        "o_result[1]": {"bits": [43]},
        # bit 44 is missing
    }
    result = _expand_bus_bits("o_result", [42, 43, 44], netnames)
    assert result == ["o_result"]


def test_expand_bus_bits_empty_bits() -> None:
    result = _expand_bus_bits("sig", [], {})
    assert result == ["sig"]


# ---------------------------------------------------------------------------
# _port_names — first fix: multi-bit ports now included
# ---------------------------------------------------------------------------


def test_port_names_includes_multi_bit_outputs(tmp_path: Path) -> None:
    data = _module(
        ports={
            "clk": {"direction": "input", "bits": [2]},
            "data": {"direction": "input", "bits": [3, 4, 5]},
            "o_result": {"direction": "output", "bits": [6, 7, 8, 9]},
        }
    )
    p = _write_json(tmp_path, data)
    out = _port_names(p, "top", "output")
    # Multi-bit port o_result must appear (first fix)
    assert "o_result" in out


def test_port_names_single_bit_still_works(tmp_path: Path) -> None:
    data = _module(
        ports={
            "A": {"direction": "input", "bits": [2]},
            "Y": {"direction": "output", "bits": [3]},
        }
    )
    p = _write_json(tmp_path, data)
    assert _port_names(p, "top", "output") == ["Y"]
    assert _port_names(p, "top", "input") == ["A"]


# ---------------------------------------------------------------------------
# _port_names expand_buses=True
# ---------------------------------------------------------------------------


def test_port_names_expand_buses_with_indexed_netnames(tmp_path: Path) -> None:
    data = _module(
        ports={"o_result": {"direction": "output", "bits": [10, 11, 12]}},
        netnames={
            "o_result": {"bits": [10, 11, 12]},
            "o_result[0]": {"bits": [10]},
            "o_result[1]": {"bits": [11]},
            "o_result[2]": {"bits": [12]},
        },
    )
    p = _write_json(tmp_path, data)
    result = _port_names(p, "top", "output", expand_buses=True)
    assert set(result) == {"o_result[0]", "o_result[1]", "o_result[2]"}
    assert len(result) == 3


def test_port_names_expand_buses_fallback_to_port_name(tmp_path: Path) -> None:
    # netnames only has bus-level entry (fastfir case)
    data = _module(
        ports={"o_result": {"direction": "output", "bits": [10, 11, 12]}},
        netnames={"o_result": {"bits": [10, 11, 12]}},
    )
    p = _write_json(tmp_path, data)
    result = _port_names(p, "top", "output", expand_buses=True)
    assert result == ["o_result"]


def test_port_names_expand_buses_single_bit_unchanged(tmp_path: Path) -> None:
    data = _module(
        ports={"Y": {"direction": "output", "bits": [5]}},
        netnames={"Y": {"bits": [5]}},
    )
    p = _write_json(tmp_path, data)
    # Single-bit ports are not expanded (no-op)
    assert _port_names(p, "top", "output", expand_buses=True) == ["Y"]


def test_port_names_expand_buses_false_returns_port_name(tmp_path: Path) -> None:
    # expand_buses=False (default) returns the port name even for multi-bit
    data = _module(
        ports={"o_result": {"direction": "output", "bits": [10, 11, 12]}},
        netnames={
            "o_result[0]": {"bits": [10]},
            "o_result[1]": {"bits": [11]},
            "o_result[2]": {"bits": [12]},
        },
    )
    p = _write_json(tmp_path, data)
    result = _port_names(p, "top", "output", expand_buses=False)
    assert result == ["o_result"]
