"""Scan ATPG through the compression decompressor (faultflow.scan.compression):
the decompressor loads every scan cell from one seed of [compression] channels
bits, once per pattern, so a compressed design applies only the loads that seed
space spans. The scan pipeline therefore (1) gives the SAT solver each cell's
seed-bit row (detection_pipeline._seed_kwargs), (2) applies every candidate as
the decompressor shifts it -- the seed's whole scan-in stream, every position
care (_decompressed) -- and (3) draws random patterns as random seeds
(_seeded_random_vectors). Rows come from ring_generator.care_bit_rows; which
shift cycle loads a cell is taken here from serialize_vector itself."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import CompressionConfig, load_config
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.compression import build_broadcast_fanout
from faultflow.scan.detection_pipeline import (
    ScanPipelineContext,
    _decompressed,
    _seed_kwargs,
    _seeded_random_vectors,
    build_los_couples,
    build_scan_pipeline_context,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.protocol import serialize_vector
from faultflow.scan.ring_generator import care_bit_rows, lookup_polynomial

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "ring"


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


def _ring_json(n_ffs: int) -> dict[str, Any]:
    """n_ffs flops in a ring: D_i = Q_{i+1} ^ B. Y = Q_0."""
    clk, b = 2, 3
    q = [10 + i for i in range(n_ffs)]
    d = [100 + i for i in range(n_ffs)]
    cells = {}
    for i in range(n_ffs):
        cells[f"u{i}"] = _cell(
            "sky130_fd_sc_hd__dfxtp_1", {"CLK": [clk], "D": [d[i]], "Q": [q[i]]}, {"Q"}
        )
        cells[f"x{i}"] = _cell(
            "sky130_fd_sc_hd__xor2_1",
            {"A": [q[(i + 1) % n_ffs]], "B": [b], "X": [d[i]]},
            {"X"},
        )
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [clk]},
                    "B": {"direction": "input", "bits": [b]},
                    "Y": {"direction": "output", "bits": [q[0]]},
                },
                "cells": cells,
                "netnames": {},
            }
        }
    }


def _context(
    tmp_path: Path, n_ffs: int, chains: int, *, compression: bool = True
) -> ScanPipelineContext:
    """A scan pipeline context for the ring, stitched into `chains` chains, with
    8-channel compression on."""
    from faultflow.runner.runner import _port_names
    from faultflow.scan import stitch_scan_json
    from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "ring.json"
    source.write_text(json.dumps(_ring_json(n_ffs)) + "\n", encoding="utf-8")
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"[design]\nnetlist = {source}\ncell_lib = {CELL_MAP}\n\n"
        f"[scan]\nchains = {chains}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, TOP)
    if compression:
        cfg = dataclasses.replace(
            cfg, compression=CompressionConfig(enabled=True, channels=8)
        )
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    result = stitch_scan_json(source, CELL_MAP, TOP, generic, scan_chains=chains)
    manifest = manifest_from_result(result, source, tmp_path / "techmap.v", None)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": hash_file(generic),
    }
    view, pseudo_port_map = build_scan_atpg_view(
        json.loads(generic.read_text(encoding="utf-8")), manifest
    )
    view_path = cfg.intermediate_dir / "scan_atpg_view.json"
    view_path.write_text(json.dumps(view) + "\n", encoding="utf-8")
    outputs = [
        p for p in _port_names(view_path, TOP, "output") if not p.startswith("__")
    ]
    return build_scan_pipeline_context(cfg, manifest, generic, pseudo_port_map, outputs)


def _rows(ctx: ScanPipelineContext, cycles: int) -> list[list[int]]:
    chains = len(ctx.manifest["scan_inputs"])
    return care_bit_rows(
        lookup_polynomial(8), build_broadcast_fanout(8, chains), cycles
    )


def _bits(mask: int) -> list[int]:
    return [k for k in range(mask.bit_length()) if (mask >> k) & 1]


def _parity(mask: int, seed: int) -> bool:
    return bin(mask & seed).count("1") % 2 == 1


def _seed_of(equations: list[tuple[int, bool]]) -> int | None:
    """A seed meeting every (row mask, value) equation, by brute force over the
    256 seeds of 8 bits -- independent of the pipeline's own GF(2) solve."""
    for seed in range(256):
        if all(_parity(mask, seed) == value for mask, value in equations):
            return seed
    return None


def _ppis(ctx: ScanPipelineContext) -> list[str]:
    return sorted(str(e["ppi_port"]) for e in ctx.pseudo_port_map.values())


def _load_vector(ctx: ScanPipelineContext, seed: int) -> dict[str, bool]:
    """B random-ish, every cell what `seed` loads into it -- by its row."""
    vector = {"B": True}
    rows = _rows(ctx, int(ctx.manifest["max_chain_length"]))
    for ppi, (chain, cycle) in _load_positions(ctx).items():
        vector[ppi] = _parity(rows[cycle][chain], seed)
    return vector


def _load_positions(ctx: ScanPipelineContext) -> dict[str, tuple[int, int]]:
    """Each cell's (chain, shift cycle) as serialize_vector loads it: the one
    position a vector with only that cell at 1 sets."""
    positions = {}
    for ppi in _ppis(ctx):
        only_ppi = {name: name == ppi for name in _ppis(ctx)}
        pattern = serialize_vector(only_ppi, ctx.pseudo_port_map, ctx.manifest)
        (position,) = [
            (chain, cycle)
            for chain, bits in pattern.load_seqs.items()
            for cycle, bit in enumerate(bits)
            if bit
        ]
        positions[ppi] = position
    return positions


@pytest.mark.unit
def test_each_cell_gets_the_row_of_the_cycle_that_loads_it(tmp_path: Path) -> None:
    """Five flops in two chains (3 and 2): the shorter chain's first shift cycle
    passes through it, so a cell's cycle is not its chain position."""
    ctx = _context(tmp_path, 5, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    assert max_len == 3
    rows = _rows(ctx, max_len + 1)
    positions = _load_positions(ctx)
    assert {cycle for _, cycle in positions.values()} == {0, 1, 2}
    assert ctx.seeded_inputs == {
        ppi: _bits(rows[cycle][chain]) for ppi, (chain, cycle) in positions.items()
    }
    # Launching on shift, a chain head's next bit is the decompressor's output
    # one cycle after the load.
    _, _, head_by_chain = build_los_couples(ctx.pseudo_port_map)
    assert ctx.seeded_heads == {
        ppi: _bits(rows[max_len][chain]) for chain, ppi in head_by_chain.items()
    }


@pytest.mark.unit
def test_seed_kwargs_only_with_compression(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "on", 5, 2)
    assert _seed_kwargs(ctx) == {"seed_width": 8, "seeded_inputs": ctx.seeded_inputs}
    assert _seed_kwargs(ctx, los=True) == {
        "seed_width": 8,
        "seeded_inputs": ctx.seeded_inputs,
        "seeded_heads": ctx.seeded_heads,
    }
    off = _context(tmp_path / "off", 5, 2, compression=False)
    assert off.seeded_inputs == {} and off.seeded_heads == {}
    assert _seed_kwargs(off) == {} and _seed_kwargs(off, los=True) == {}


@pytest.mark.unit
def test_decompressed_pattern_is_its_seeds_whole_stream(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    ctx = _context(tmp_path, 5, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    rows = _rows(ctx, max_len)
    vector = _load_vector(ctx, 0b1011_0110)
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    pattern = _decompressed(_load_core(), ctx, vector, loaded, {})

    positions = [(c, t) for c in sorted(loaded.load_seqs) for t in range(max_len)]
    assert pattern.load_care == tuple(positions)
    # The loaded cells keep their values; the cycle that passes through the
    # short chain is the decompressor's too -- every bit from ONE seed.
    for ppi, (chain, cycle) in _load_positions(ctx).items():
        assert pattern.load_seqs[chain][cycle] == vector[ppi]
    seed = _seed_of([(rows[t][c], pattern.load_seqs[c][t]) for c, t in positions])
    assert seed is not None
    assert pattern.capture_pi_values == loaded.capture_pi_values
    assert pattern.expected_unload == loaded.expected_unload


@pytest.mark.unit
def test_decompressed_refuses_a_load_no_seed_makes(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Ten cells, eight seed bits: some loads are out of reach. A candidate with
    one would be a bug upstream (the solver and random fill draw seeds)."""
    from faultflow.runner.runner import _load_core

    ctx = _context(tmp_path, 10, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    rows = _rows(ctx, max_len)
    positions = _load_positions(ctx)
    ppis = sorted(positions)
    unreachable = None
    for load in range(1 << len(ppis)):
        equations = [
            (rows[cycle][chain], bool((load >> i) & 1))
            for i, (chain, cycle) in enumerate(positions[p] for p in ppis)
        ]
        if _seed_of(equations) is None:
            unreachable = {p: bool((load >> i) & 1) for i, p in enumerate(ppis)}
            break
    assert unreachable is not None
    vector = {"B": False, **unreachable}
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    with pytest.raises(ScanError, match="seed"):
        _decompressed(_load_core(), ctx, vector, loaded, {})


@pytest.mark.unit
def test_decompressed_launch_shift_bits_come_from_the_same_seed(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    ctx = _context(tmp_path, 10, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    rows = _rows(ctx, max_len + 1)
    seed = 0b0110_1001
    vector = _load_vector(ctx, seed)
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    heads = {chain: _parity(rows[max_len][chain], seed) for chain in loaded.load_seqs}
    pattern = _decompressed(core, ctx, vector, loaded, heads)
    assert (
        _seed_of(
            [
                (rows[t][c], pattern.load_seqs[c][t])
                for c in heads
                for t in range(max_len)
            ]
            + [(rows[max_len][c], bit) for c, bit in heads.items()]
        )
        is not None
    )
    # A launch-shift bit no seed of this load gives is refused.
    load_equations = [
        (rows[t][c], loaded.load_seqs[c][t]) for c, t in _load_positions(ctx).values()
    ]
    refused = 0
    for chain in heads:
        flipped = {**heads, chain: not heads[chain]}
        equations = load_equations + [
            (rows[max_len][c], bit) for c, bit in flipped.items()
        ]
        if _seed_of(equations) is None:
            refused += 1
            with pytest.raises(ScanError, match="seed"):
                _decompressed(core, ctx, vector, loaded, flipped)
        else:
            _decompressed(core, ctx, vector, loaded, flipped)
    assert refused > 0


@pytest.mark.unit
def test_seeded_random_vectors_are_loads_of_one_seed(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Twelve cells, eight seed bits: random loads would almost never be one."""
    from faultflow.runner.runner import _atpg_pi_names, _load_core

    core = _load_core()
    ctx = _context(tmp_path, 12, 3)
    max_len = int(ctx.manifest["max_chain_length"])
    rows = _rows(ctx, max_len + 1)
    order = _atpg_pi_names(ctx.cfg.intermediate_dir / "scan_atpg_view.json", TOP)
    positions = _load_positions(ctx)

    drawn = _seeded_random_vectors(core, order, ctx, 32, los=True)
    assert len(drawn) == 32
    assert drawn == _seeded_random_vectors(core, order, ctx, 32, los=True)
    assert len({tuple(sorted(v.items())) for v, _ in drawn}) > 16
    assert {v["B"] for v, _ in drawn} == {False, True}
    for vector, heads in drawn:
        assert set(vector) == set(order)
        equations = [(rows[t][c], vector[p]) for p, (c, t) in positions.items()]
        equations += [(rows[max_len][c], bit) for c, bit in heads.items()]
        assert set(heads) == set(range(len(ctx.manifest["scan_inputs"])))
        assert _seed_of(equations) is not None
    # Without launch-on-shift there are no launch-shift bits.
    assert all(h == {} for _, h in _seeded_random_vectors(core, order, ctx, 4))
