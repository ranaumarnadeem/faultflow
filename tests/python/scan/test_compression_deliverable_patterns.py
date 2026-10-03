"""Under scan compression, a tester can only load what the decompressor makes
from a seed, so every pattern a compressed campaign accepts must be such a load,
and its credit must come from that load.

The decompressor (faultflow.scan.compression) reseeds an 8-bit LFSR from the
channels at the start of each load and runs it through a phase shifter: a load
is fixed by its seed. This design's 13 flops stitch into chains of 5, 4 and 4,
so a load has 15 shift positions (two of them padding that shifts straight
through), and at most 2**8 of the 2**15 loads can be applied. A tester (warptap's
faultflow_compression) solves a seed from a pattern's load_care positions, or
from every load position when it has none, and compares the unload against the
pattern's expected_unload: the load that seed makes has to be the pattern's own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from faultflow.runner.runner import _load_core
from faultflow.scan.compression import build_broadcast_fanout
from faultflow.scan.pattern_export import scan_pattern_from_dict
from faultflow.scan.protocol import ScanPattern
from faultflow.scan.ring_generator import (
    bitmask_to_index_list,
    care_bit_rows,
    lookup_polynomial,
)
from scan_credit import credit_not_reproduced

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
FLOPS = 13
WIDTH = 8


def _cell(cell_type: str, directions: dict[str, str], conns: dict[str, int]) -> dict:
    return {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {},
        "port_directions": directions,
        "connections": {pin: [net] for pin, net in conns.items()},
    }


def _nand_ring_json() -> dict[str, object]:
    """13 flops, each loading the NAND of its own Q and the next flop's (the
    last one's NAND takes IN): a fault on a NAND input needs both inputs
    loaded at 1, so detection depends on the load."""
    clk, inp = 2, 3
    q = [10 + i for i in range(FLOPS)]
    d = [30 + i for i in range(FLOPS)]
    cells: dict[str, dict] = {}
    netnames: dict[str, dict] = {
        "CLK": {"hide_name": 0, "bits": [clk], "attributes": {}},
        "IN": {"hide_name": 0, "bits": [inp], "attributes": {}},
    }
    for i in range(FLOPS):
        cells[f"u{i}"] = _cell(
            "sky130_fd_sc_hd__dfxtp_1",
            {"CLK": "input", "D": "input", "Q": "output"},
            {"CLK": clk, "D": d[i], "Q": q[i]},
        )
        cells[f"g{i}"] = _cell(
            "sky130_fd_sc_hd__nand2_1",
            {"A": "input", "B": "input", "Y": "output"},
            {"A": q[i], "B": q[i + 1] if i + 1 < FLOPS else inp, "Y": d[i]},
        )
        netnames[f"q{i}"] = {"hide_name": 0, "bits": [q[i]], "attributes": {}}
        netnames[f"d{i}"] = {"hide_name": 0, "bits": [d[i]], "attributes": {}}
    return {
        "modules": {
            "core_top": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [clk]},
                    "IN": {"direction": "input", "bits": [inp]},
                    "OUT": {"direction": "output", "bits": [q[-1]]},
                },
                "cells": cells,
                "netnames": netnames,
            }
        }
    }


def _write_config(path: Path, netlist: Path, model: str) -> Path:
    fault_model = (
        "model = transition\nlaunch = loc" if model == "loc" else "model = stuck_at"
    )
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}
liberty = {LIBERTY}

[fault_model]
{fault_model}
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
output = missing.test
max_rounds = 20
preflight = false

[scan]
chains = 3

[compression]
enabled = true
channels = {WIDTH}
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


def _applied_load(
    core: object, pattern: ScanPattern, rows: list[list[int]]
) -> dict[int, list[bool]] | None:
    """The load a tester gives `pattern` through the decompressor: the seed
    solved from its load_care positions, or from every load position without
    them, run through the LFSR and the phase shifter. None if no seed
    reproduces those positions."""
    care = None if pattern.load_care is None else set(pattern.load_care)
    positions = [
        (chain, cycle)
        for chain, bits in pattern.load_seqs.items()
        for cycle in range(len(bits))
        if care is None or (chain, cycle) in care
    ]
    solved = core.solve_xor_broadcast(  # type: ignore[attr-defined]
        WIDTH,
        [bitmask_to_index_list(rows[cycle][chain]) for chain, cycle in positions],
        [
            (index, bool(pattern.load_seqs[chain][cycle]))
            for index, (chain, cycle) in enumerate(positions)
        ],
    )
    if not solved["ok"]:
        return None
    seed = sum(1 << bit for bit, value in enumerate(solved["channels"]) if value)
    return {
        chain: [
            bin(rows[cycle][chain] & seed).count("1") % 2 == 1
            for cycle in range(len(bits))
        ]
        for chain, bits in pattern.load_seqs.items()
    }


@pytest.mark.integration
@pytest.mark.sequential
@pytest.mark.parametrize("model", ["stuck_at", "loc"])
def test_compressed_campaign_patterns_are_loads_the_decompressor_makes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_cpp_core: None,
    model: str,
) -> None:
    # GIVEN a design with more load positions than seed bits, compressed
    monkeypatch.chdir(tmp_path)
    netlist = tmp_path / "core_top.json"
    netlist.write_text(json.dumps(_nand_ring_json(), indent=2), encoding="utf-8")
    cfg_path = _write_config(tmp_path / "config.ofs", netlist, model)
    assert main(["scan", "--top", "core_top", "-c", str(cfg_path), "--no-techmap"]) == 0
    assert main(["scan-check", "--top", "core_top", "-c", str(cfg_path)]) == 0
    cfg = load_config(cfg_path, "core_top")
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    chains = len(manifest["scan_inputs"])
    max_chain_length = int(manifest["max_chain_length"])
    assert chains * max_chain_length > WIDTH

    # WHEN the scan campaign runs: random fill, then SAT
    patterns_path = tmp_path / "patterns.json"
    assert (
        main(
            [
                "sim",
                "--scan",
                "--top",
                "core_top",
                "-c",
                str(cfg_path),
                "--export-patterns",
                str(patterns_path),
            ]
        )
        == 0
    )

    # THEN every pattern is the load its seed makes...
    core = _load_core()
    rows = care_bit_rows(
        lookup_polynomial(WIDTH),
        build_broadcast_fanout(WIDTH, chains),
        max_chain_length,
    )
    patterns = [
        scan_pattern_from_dict(raw)
        for raw in json.loads(patterns_path.read_text(encoding="utf-8"))
    ]
    assert patterns
    not_applicable = [
        index
        for index, pattern in enumerate(patterns)
        if _applied_load(core, pattern, rows) != pattern.load_seqs
    ]
    assert not_applicable == [], (
        f"{len(not_applicable)} of {len(patterns)} patterns load something the "
        f"decompressor can't make from their seed: {not_applicable}"
    )
    # ...and those loads detect every fault the campaign credits.
    assert credit_not_reproduced(cfg, patterns_path, loc=model == "loc") == []
