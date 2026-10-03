"""Scan ATPG through the compression decompressor, end to end: `ff.py scan` ->
`scan-check` -> `scan-compress` -> `sim --scan --export-patterns` on a design
whose 16 scan cells outnumber the 8 seed bits that load them, in five chains
of unequal length. Its output Y is the AND of all 16 cells, so Y stuck-at-0
needs a load no seed makes: compression_unresolved, never redundant.

Every credited fault must be detected on the compressed chip itself -- each
exported pattern's seed solved the way warptap's retarget solves it, held on
the channel bus, the synthesized decompressor loading the chains
(scan_credit.compressed_credit_not_reproduced) -- and every pattern, random
fill included, must be one seed's whole load."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from scan_credit import compressed_credit_not_reproduced, credit_not_reproduced
from warptap_helpers import skip_unless_warptap

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "comp_and"
N_FFS = 16
CLK, A, B, Y, Z = 2, 3, 4, 5, 6


def _q(i: int) -> int:
    return 10 + i


def _cell(kind: str, conns: dict[str, list[int]], outputs: set[str]) -> dict:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {
            pin: "output" if pin in outputs else "input" for pin in conns
        },
        "connections": conns,
    }


def _design() -> dict[str, Any]:
    """D_i = Q_{i+1} ^ B; Y = AND of every Q (an and4 tree); Z = Q_0 ^ A."""
    cells: dict[str, dict] = {}
    for i in range(N_FFS):
        d = 100 + i
        cells[f"u{i}"] = _cell(
            "sky130_fd_sc_hd__dfxtp_1", {"CLK": [CLK], "D": [d], "Q": [_q(i)]}, {"Q"}
        )
        cells[f"x{i}"] = _cell(
            "sky130_fd_sc_hd__xor2_1",
            {"A": [_q((i + 1) % N_FFS)], "B": [B], "X": [d]},
            {"X"},
        )
    for k in range(4):
        cells[f"a{k}"] = _cell(
            "sky130_fd_sc_hd__and4_1",
            {pin: [_q(4 * k + j)] for j, pin in enumerate("ABCD")} | {"X": [200 + k]},
            {"X"},
        )
    cells["ay"] = _cell(
        "sky130_fd_sc_hd__and4_1",
        {pin: [200 + j] for j, pin in enumerate("ABCD")} | {"X": [Y]},
        {"X"},
    )
    cells["xz"] = _cell(
        "sky130_fd_sc_hd__xor2_1", {"A": [_q(0)], "B": [A], "X": [Z]}, {"X"}
    )
    ports = {
        "CLK": {"direction": "input", "bits": [CLK]},
        "A": {"direction": "input", "bits": [A]},
        "B": {"direction": "input", "bits": [B]},
        "Y": {"direction": "output", "bits": [Y]},
        "Z": {"direction": "output", "bits": [Z]},
    }
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {
                    name: {"hide_name": 0, "bits": port["bits"], "attributes": {}}
                    for name, port in ports.items()
                },
            }
        }
    }


def _flow(tmp_path: Path, *, fault_model: str = "", atpg: str = "") -> Any:
    """The flow on the design; `fault_model` / `atpg` are extra keys of those
    sections."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    source = tmp_path / f"{TOP}.json"
    source.write_text(json.dumps(_design(), indent=2) + "\n", encoding="utf-8")
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"[design]\nnetlist = {source}\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        f"[fault_model]\ncollapsing = false\n{fault_model}\n"
        "[simulation]\nunsupported_cells = fail\n\n"
        # Four random patterns, so SAT has faults left to find tests for.
        f"[atpg]\nmode = comb\nmax_rounds = 20\nrandom_vectors = 4\n{atpg}\n"
        "[scan]\nchains = 5\n\n"
        "[compression]\nenabled = true\nchannels = 8\n",
        encoding="utf-8",
    )
    patterns = tmp_path / "patterns.json"
    for step in (
        ["scan", "--no-techmap"],
        ["scan-check"],
        ["scan-compress"],
        ["sim", "--scan", "--export-patterns", str(patterns)],
    ):
        assert main([*step, "--top", TOP, "-c", str(cfg_path)]) == 0, step
    return load_config(cfg_path, TOP)


def _rows_and_seed_solver(cfg: Any) -> tuple[Any, Any, int]:
    """warptap's model of the decompressor -- its own port of the LFSR and the
    GF(2) solve -- and the solve its retarget runs on each exported pattern."""
    skip_unless_warptap("warptap.faultflow_compression")
    from warptap.faultflow_compression import (
        care_bit_rows,
        polynomial_from_manifest,
        solve_pattern_seed,
    )

    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    poly = polynomial_from_manifest(compression)
    max_chain_length = int(manifest["max_chain_length"])
    rows = care_bit_rows(poly, compression["phase_shifter_taps"], max_chain_length + 1)

    def seed_of(number: int, raw: dict[str, Any]) -> int:
        care = raw["load_care"]
        return int(
            solve_pattern_seed(
                raw["load_seqs"],
                rows[:max_chain_length],
                poly.width,
                number,
                None if care is None else {(int(c), int(t)) for c, t in care},
            )
        )

    return rows, seed_of, max_chain_length


def _parity(mask: int, seed: int) -> bool:
    return bin(mask & seed).count("1") % 2 == 1


def _campaign_rows(cfg: Any, query: str) -> list[Any]:
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        (campaign,) = conn.execute(
            "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
        ).fetchone()
        return conn.execute(query, (campaign,)).fetchall()
    finally:
        conn.close()


def _assert_every_pattern_is_one_seeds_load(cfg: Any, patterns: Path) -> None:
    """Even read the strictest way -- every load position a hard constraint,
    as warptap reads a pattern without load_care -- each pattern is one
    seed's load, and its load_care names every position."""
    rows, seed_of, max_chain_length = _rows_and_seed_solver(cfg)
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    assert exported
    for number, raw in enumerate(exported):
        chains = sorted(int(c) for c in raw["load_seqs"])
        assert sorted(map(tuple, raw["load_care"])) == [
            (c, t) for c in chains for t in range(max_chain_length)
        ], number
        seed = seed_of(number, {**raw, "load_care": None})
        for chain in chains:
            assert raw["load_seqs"][str(chain)] == [
                _parity(rows[t][chain], seed) for t in range(max_chain_length)
            ], (number, chain)


def _assert_nothing_rejected_for_compression(cfg: Any) -> None:
    reasons = {
        row["reason_code"]
        for row in _campaign_rows(
            cfg, "SELECT reason_code FROM candidate_rejections WHERE campaign_id = ?"
        )
    }
    assert "compression_unsatisfiable" not in reasons
    accepted = _campaign_rows(
        cfg,
        "SELECT source FROM atpg_candidates WHERE campaign_id = ? "
        "AND status = 'accepted'",
    )
    assert {row["source"] for row in accepted} == {"random", "sat"}


def _all_ones_is_loadable(cfg: Any) -> bool:
    """Brute force over the 256 seeds: does any load every cell with 1?"""
    rows, _, max_chain_length = _rows_and_seed_solver(cfg)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    cycles = {
        chain["index"]: range(max_chain_length - chain["length"], max_chain_length)
        for chain in manifest["chains"]
    }
    return any(
        all(_parity(rows[t][c], seed) for c, ts in cycles.items() for t in ts)
        for seed in range(256)
    )


@pytest.mark.integration
@pytest.mark.parametrize("workers", [1, 2])
def test_stuck_at_through_the_decompressor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _flow(tmp_path, atpg=f"workers = {workers}\n")
    patterns = tmp_path / "patterns.json"

    _assert_nothing_rejected_for_compression(cfg)
    _assert_every_pattern_is_one_seeds_load(cfg, patterns)
    assert credit_not_reproduced(cfg, patterns) == []
    _, seed_of, _ = _rows_and_seed_solver(cfg)
    assert compressed_credit_not_reproduced(cfg, patterns, seed_of) == {
        "not_reproduced": [],
        "golden": [],
    }

    # Y stuck-at-0 needs all 16 cells at 1, which no seed loads: testable,
    # but not through this decompressor.
    assert not _all_ones_is_loadable(cfg)
    (y_sa0,) = _campaign_rows(
        cfg,
        "SELECT status, compression_unresolved FROM faults WHERE campaign_id = ? "
        f"AND fault_site_key = 'net:{Y}:stem' AND fault_type = 'sa0'",
    )
    assert y_sa0["status"] == "undetected"
    assert y_sa0["compression_unresolved"] == 1
    report = json.loads(cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["compression_unresolved"] >= 1


@pytest.mark.integration
def test_launch_on_capture_through_the_decompressor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _flow(tmp_path, fault_model="model = transition\nlaunch = loc\n")
    patterns = tmp_path / "patterns.json"
    _assert_nothing_rejected_for_compression(cfg)
    _assert_every_pattern_is_one_seeds_load(cfg, patterns)
    assert credit_not_reproduced(cfg, patterns, loc=True) == []


@pytest.mark.integration
def test_launch_on_shift_scan_in_bits_come_from_the_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launching on shift, the decompressor keeps running into the launch
    shift: each chain's launch scan-in bit is its output one cycle after the
    load, from the same seed. Every accepted candidate's key is V1 followed
    by those bits in head-port order."""
    from faultflow.runner.runner import _atpg_pi_names
    from faultflow.scan.detection_pipeline import build_los_couples

    monkeypatch.chdir(tmp_path)
    cfg = _flow(tmp_path, fault_model="model = transition\nlaunch = los\n")
    _assert_nothing_rejected_for_compression(cfg)
    _assert_every_pattern_is_one_seeds_load(cfg, tmp_path / "patterns.json")

    rows, _, max_chain_length = _rows_and_seed_solver(cfg)
    view = cfg.intermediate_dir / "scan_atpg_view.json"
    order = _atpg_pi_names(view, TOP)
    pseudo = json.loads(
        (cfg.intermediate_dir / "scan_pseudo_port_map.json").read_text(encoding="utf-8")
    )
    _, head_ports, head_by_chain = build_los_couples(pseudo)
    chain_of_head = {port: chain for chain, port in head_by_chain.items()}
    cells = {
        str(e["ppi_port"]): (int(e["chain_id"]), int(e["position_in_chain"]))
        for e in pseudo.values()
    }
    accepted = _campaign_rows(
        cfg,
        "SELECT pattern FROM atpg_candidates WHERE campaign_id = ? "
        "AND status = 'accepted'",
    )
    assert accepted
    for row in accepted:
        key = str(row["pattern"])
        assert len(key) == len(order) + len(head_ports)
        v1 = dict(zip(order, (bit == "1" for bit in key)))
        heads = {
            chain_of_head[port]: key[len(order) + i] == "1"
            for i, port in enumerate(head_ports)
        }
        equations = [
            (rows[max_chain_length - 1 - pos][chain], v1[ppi])
            for ppi, (chain, pos) in cells.items()
        ] + [(rows[max_chain_length][chain], bit) for chain, bit in heads.items()]
        assert any(
            all(_parity(mask, seed) == value for mask, value in equations)
            for seed in range(256)
        ), key
