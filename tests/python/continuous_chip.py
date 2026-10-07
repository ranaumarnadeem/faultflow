"""Tiny scanned cores behind a continuous-flow decompressor, composed by Yosys as
scan-compress composes a chip (faultflow.scan.compression), for the decompressor's
tests: the model, the cells and the functional check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from faultflow.scan.compression import (
    ContinuousDecompressor,
    _compose,
    decompressor_wrapper_verilog,
    insert_continuous_compression,
)

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "core_top"
COMPOSED_TOP = f"{TOP}_compressed"


def _scan_ff(clk: int, d: int, sdi: int, se: int, q: int) -> dict[str, Any]:
    """A generic scan flop as ``ff.py scan`` writes it: a public (escaped) cell
    type, so Yosys reads it and the scan techmap maps it to sdfxtp."""
    return {
        "hide_name": 0,
        "type": "\\$scanff_faultflow",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            "CLK": "input",
            "D": "input",
            "SDI": "input",
            "SE": "input",
            "Q": "output",
        },
        "connections": {"CLK": [clk], "D": [d], "SDI": [sdi], "SE": [se], "Q": [q]},
    }


def scanned_core(chain_lengths: list[int]) -> dict[str, Any]:
    """A scanned core: chain k is scan_in_k -> cell 0 -> ... -> scan_out_k (cell 0
    the head), its cells' functional D one shared input."""
    ports: dict[str, Any] = {
        "CLK": {"direction": "input", "bits": [2]},
        "D": {"direction": "input", "bits": [3]},
        "scan_en": {"direction": "input", "bits": [4]},
    }
    cells: dict[str, Any] = {}
    net = 5
    for chain, length in enumerate(chain_lengths):
        previous = net
        ports[f"scan_in_{chain}"] = {"direction": "input", "bits": [previous]}
        net += 1
        for position in range(length):
            cells[f"u{chain}_{position}"] = _scan_ff(2, 3, previous, 4, net)
            previous = net
            net += 1
        ports[f"scan_out_{chain}"] = {"direction": "output", "bits": [previous]}
    return {
        "modules": {TOP: {"attributes": {"top": 1}, "ports": ports, "cells": cells}}
    }


def scan_in_ports(chain_lengths: list[int]) -> list[str]:
    return [f"scan_in_{k}" for k in range(len(chain_lengths))]


def compose(
    tmp_path: Path,
    chain_lengths: list[int],
    channels: int,
    *,
    built: ContinuousDecompressor | None = None,
    mutate: Callable[[str], str] | None = None,
) -> tuple[Path, ContinuousDecompressor]:
    """The composed chip's JSON and its decompressor. ``built``: synthesize that
    decompressor instead of insert_continuous_compression's own; ``mutate``: edit
    the wrapper RTL first (it must change). Returns the decompressor asked for --
    the one insert_continuous_compression makes for ``channels`` -- whatever was
    built."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    core_json = tmp_path / "core.json"
    core = scanned_core(chain_lengths)
    core_json.write_text(json.dumps(core), encoding="utf-8")
    output = tmp_path / "compressed.json"
    composed, wanted = insert_continuous_compression(
        core_json,
        TOP,
        scan_in_ports(chain_lengths),
        channels,
        LIBERTY,
        output,
        workdir=tmp_path / "work",
        clock_port="CLK",
        scan_enable_port="scan_en",
    )
    if built is None and mutate is None:
        return composed, wanted
    rtl = decompressor_wrapper_verilog(
        core, TOP, built or wanted, COMPOSED_TOP, "core_inst"
    )
    if mutate is not None:
        changed = mutate(rtl)
        assert changed != rtl, "the mutation changed nothing"
        rtl = changed
    _compose(
        rtl,
        COMPOSED_TOP,
        core_json,
        TOP,
        LIBERTY,
        output,
        tmp_path / "work_built",
        "core_inst",
    )
    return output, wanted


def top_module(composed: Path, top: str = COMPOSED_TOP) -> dict[str, Any]:
    return dict(json.loads(composed.read_text(encoding="utf-8"))["modules"][top])


def cell_map() -> dict[str, Any]:
    return dict(json.loads(CELL_MAP.read_text(encoding="utf-8")))
