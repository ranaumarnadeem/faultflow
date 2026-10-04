"""The scan compression and compaction channels' port names: set in the .ofs, never a
port the design already has, and never a TAP pin for ff.py jtag."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.jtag.command import JtagError, run_jtag
from faultflow.scan.compaction import build_compactor_fanout, compactor_wrapper_verilog
from faultflow.scan.compression import ring_generator_wrapper_verilog
from faultflow.scan.ring_generator import LfsrPolynomial

pytestmark = pytest.mark.unit
POLY = LfsrPolynomial(2, frozenset({1}))


def _core() -> dict[str, Any]:
    """Two chains, and a TAP's tdi and tdo."""
    ports = {
        "clk": ("input", [2]),
        "scan_en": ("input", [3]),
        "tdi": ("input", [4]),
        "tdo": ("output", [5]),
        "scan_in_0": ("input", [8]),
        "scan_in_1": ("input", [9]),
        "scan_out_0": ("output", [10]),
        "scan_out_1": ("output", [11]),
    }
    return {
        "modules": {
            "core_top": {
                "ports": {
                    name: {"direction": d, "bits": bits}
                    for name, (d, bits) in ports.items()
                },
                "cells": {},
                "netnames": {},
            }
        }
    }


def test_a_channel_bus_named_like_a_design_port_is_refused() -> None:
    scan_in, scan_out = ["scan_in_0", "scan_in_1"], ["scan_out_0", "scan_out_1"]
    with pytest.raises(ValueError, match="channel port 'tdi' is a port of"):
        ring_generator_wrapper_verilog(
            _core(), "core_top", scan_in, POLY, [[0], [0, 1]], "wrap"
        )
    rtl = ring_generator_wrapper_verilog(
        _core(), "core_top", scan_in, POLY, [[0], [0, 1]], "wrap", channel_port="cs"
    )
    assert "input [1:0] cs;" in rtl and "input tdi;" in rtl
    fanout = build_compactor_fanout(2, len(scan_out))
    with pytest.raises(ValueError, match="channel port 'tdo' is a port of"):
        compactor_wrapper_verilog(_core(), "core_top", scan_out, fanout, "wrap")
    rtl = compactor_wrapper_verilog(
        _core(), "core_top", scan_out, fanout, "wrap", channel_port="co"
    )
    assert "output [1:0] co;" in rtl and "output tdo;" in rtl


def _ofs(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chip.ofs"
    path.write_text(f"[design]\nnetlist = chip.json\n\n{text}", encoding="utf-8")
    return path


def test_the_channel_ports_come_from_the_ofs(tmp_path: Path) -> None:
    plain = load_config(_ofs(tmp_path, ""), "chip")
    assert (plain.compression.channel_port, plain.compaction.channel_port) == (
        "tdi",
        "tdo",
    )
    named = load_config(
        _ofs(
            tmp_path,
            "[compression]\nchannel_port = comp_si\n\n"
            "[compaction]\nchannel_port = comp_so\n",
        ),
        "chip",
    )
    assert (named.compression.channel_port, named.compaction.channel_port) == (
        "comp_si",
        "comp_so",
    )
    with pytest.raises(ConfigError, match="channel_port must be a port name"):
        load_config(_ofs(tmp_path, "[compaction]\nchannel_port = 2x\n"), "chip")


def test_ff_jtag_refuses_only_a_channel_named_like_a_tap_pin(tmp_path: Path) -> None:
    clash = load_config(
        _ofs(tmp_path, "[compression]\nenabled = true\nchannels = 8\n"), "chip"
    )
    with pytest.raises(JtagError, match="channel_port is tdi, a TAP port's name"):
        run_jtag(clash)
    apart = load_config(
        _ofs(
            tmp_path,
            "[compression]\nenabled = true\nchannels = 8\nchannel_port = comp_si\n",
        ),
        "chip",
    )
    with pytest.raises(JtagError) as refused:
        run_jtag(apart)  # gets past the channels; nothing to grade here
    assert "channel" not in str(refused.value)
