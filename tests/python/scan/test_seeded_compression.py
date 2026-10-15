"""Scan ATPG through the continuous decompressor (faultflow.scan.compression): the
channels take a word on every shift, so a load is a point in a space of
channels x (warm-up + chain length) free bits, and a compressed design applies only
the loads that space spans. The scan pipeline therefore (1) gives the SAT solver
each cell's scan-in as a short XOR over a straight-line program of the ring
(detection_pipeline._seed_kwargs), (2) applies every candidate as the decompressor
shifts it -- the channel words of every shift of the load, every position care
(_decompressed) -- and (3) draws random patterns as random channel words
(_seeded_random_vectors). The expected bits come from the flat model the RTL is
checked against (ring_generator.continuous_scan_in_rows); which shift loads a cell is
taken here from serialize_vector itself."""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import CompressionConfig, load_config
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.compression import build_phase_shifter
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
from faultflow.scan.ring_generator import (
    continuous_scan_in_bits,
    continuous_scan_in_rows,
    lookup_polynomial,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "ring"
RING = 16
WARMUP = 3


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
    tmp_path: Path,
    n_ffs: int,
    chains: int,
    *,
    channels: int = 8,
    compression: bool = True,
) -> ScanPipelineContext:
    """A scan pipeline context for the ring, stitched into `chains` chains, with a
    16-stage ring, `channels` channels and a warm-up of 3 shifts on."""
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
            cfg,
            compression=CompressionConfig(
                enabled=True, channels=channels, ring_width=RING, warmup=WARMUP
            ),
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


def _shifts(ctx: ScanPipelineContext) -> int:
    """A load's shifts: the warm-up and the longest chain's."""
    return WARMUP + int(ctx.manifest["max_chain_length"])


def _rows(ctx: ScanPipelineContext, channels: int) -> list[list[int]]:
    """The flat model: rows[t][chain] is that chain's scan-in at shift t as a mask over
    the free bits (bit t*channels + k: channel k's bit at shift t), the launch-on-shift
    shift's included."""
    chains = len(ctx.manifest["scan_inputs"])
    return continuous_scan_in_rows(
        lookup_polynomial(RING),
        build_phase_shifter(RING, chains),
        channels,
        _shifts(ctx) + 1,
    )


def _expand(ctx: ScanPipelineContext, variables: list[int]) -> int:
    """The mask over the free bits that the XOR of program `variables` is."""
    program = ctx.seed_program
    assert program is not None
    masks = [1 << i for i in range(program.seed_width)]
    for operands in program.defs:
        mask = 0
        for i in operands:
            mask ^= masks[i]
        masks.append(mask)
    result = 0
    for v in variables:
        result ^= masks[v]
    return result


def _parity(mask: int, free: int) -> bool:
    return bin(mask & free).count("1") % 2 == 1


def _ppis(ctx: ScanPipelineContext) -> list[str]:
    return sorted(str(e["ppi_port"]) for e in ctx.pseudo_port_map.values())


def _load_positions(ctx: ScanPipelineContext) -> dict[str, tuple[int, int]]:
    """Each cell's (chain, load cycle) as serialize_vector loads it: the one
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


def _load_vector(ctx: ScanPipelineContext, channels: int, free: int) -> dict[str, bool]:
    """B at 1, every cell what the free bits `free` load into it -- by its row."""
    vector = {"B": True}
    rows = _rows(ctx, channels)
    for ppi, (chain, cycle) in _load_positions(ctx).items():
        vector[ppi] = _parity(rows[WARMUP + cycle][chain], free)
    return vector


def _free_bits(free: int, width: int) -> list[bool]:
    return [bool((free >> i) & 1) for i in range(width)]


def _consistent(equations: list[tuple[int, bool]]) -> bool:
    """Whether some free bits meet every (row mask, value) equation: Gaussian
    elimination, independent of the pipeline's own solve."""
    pivots: dict[int, tuple[int, bool]] = {}
    for mask, value in equations:
        while mask:
            low = mask & -mask
            if low not in pivots:
                pivots[low] = (mask, value)
                break
            mask ^= pivots[low][0]
            value ^= pivots[low][1]
        else:
            if value:
                return False
    return True


def _dependent_cells(rows: dict[str, int]) -> list[str]:
    """Cells whose rows XOR to 0 (there are some once cells outnumber the free bits'
    dimensions): setting one of them 1 and the rest 0 is a load no free bits make."""
    basis: dict[int, tuple[int, set[str]]] = {}
    for name, mask in rows.items():
        combo = {name}
        while mask:
            low = mask & -mask
            if low not in basis:
                basis[low] = (mask, combo)
                break
            mask ^= basis[low][0]
            combo = combo ^ basis[low][1]
        else:
            return sorted(combo)
    raise AssertionError("the cells' rows are independent")


@pytest.mark.unit
def test_each_cell_gets_the_scan_in_of_the_shift_that_loads_it(tmp_path: Path) -> None:
    """Five flops in two chains (3 and 2): the shorter chain's first load cycle passes
    through it, so a cell's cycle is not its chain position."""
    ctx = _context(tmp_path, 5, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    assert max_len == 3
    rows = _rows(ctx, 8)
    positions = _load_positions(ctx)
    assert {cycle for _, cycle in positions.values()} == {0, 1, 2}
    # The warm-up shifts come first: the cell loaded on load cycle t takes shift
    # WARMUP + t's scan-in.
    assert {
        ppi: _expand(ctx, variables) for ppi, variables in ctx.seeded_inputs.items()
    } == {ppi: rows[WARMUP + cycle][chain] for ppi, (chain, cycle) in positions.items()}
    # Launching on shift, a chain head's next bit is the scan-in one shift after the
    # load.
    _, _, head_by_chain = build_los_couples(ctx.pseudo_port_map)
    assert {
        ppi: _expand(ctx, variables) for ppi, variables in ctx.seeded_heads.items()
    } == {ppi: rows[WARMUP + max_len][chain] for chain, ppi in head_by_chain.items()}


@pytest.mark.unit
def test_seed_kwargs_only_with_compression(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "on", 5, 2)
    program = ctx.seed_program
    assert program is not None
    assert program.seed_width == 8 * _shifts(ctx)
    assert _seed_kwargs(ctx) == {
        "seed_width": program.seed_width,
        "seed_defs": program.defs,
        "seeded_inputs": ctx.seeded_inputs,
    }
    assert _seed_kwargs(ctx, los=True) == {
        "seed_width": program.seed_width,
        "seed_defs": program.defs,
        "seeded_inputs": ctx.seeded_inputs,
        "seeded_heads": ctx.seeded_heads,
    }
    off = _context(tmp_path / "off", 5, 2, compression=False)
    assert off.decompressor is None and off.seed_program is None
    assert off.seeded_inputs == {} and off.seeded_heads == {}
    assert _seed_kwargs(off) == {} and _seed_kwargs(off, los=True) == {}


@pytest.mark.unit
def test_the_context_has_the_decompressor_the_config_describes(tmp_path: Path) -> None:
    ctx = _context(tmp_path, 5, 2, channels=4)
    assert ctx.decompressor is not None
    assert ctx.decompressor.num_channels == 4
    assert (ctx.decompressor.polynomial.width, ctx.decompressor.warmup) == (
        RING,
        WARMUP,
    )
    assert ctx.decompressor.phase_shifter_taps == build_phase_shifter(RING, 2)


@pytest.mark.unit
def test_a_manifest_from_another_decompressor_is_refused(tmp_path: Path) -> None:
    """The scan manifest records the decompressor scan-compress composed; an ATPG
    configured for another would grade loads the chip does not make."""
    from faultflow.scan.compression import build_continuous_decompressor

    ctx = _context(tmp_path / "ctx", 5, 2)
    other = build_continuous_decompressor(
        [str(p) for p in ctx.manifest["scan_inputs"]], 8, ring_width=32, warmup=WARMUP
    )
    manifest = {
        **ctx.manifest,
        "compression": {"enabled": True, **other.manifest_entry()},
    }
    with pytest.raises(ScanError, match="scan-compress"):
        build_scan_pipeline_context(
            ctx.cfg,
            manifest,
            ctx.generic_json,
            ctx.pseudo_port_map,
            ctx.functional_output_order,
        )
    one_shot = {**manifest, "compression": {"enabled": True, "num_channels": 8}}
    with pytest.raises(ScanError, match="scan-compress"):
        build_scan_pipeline_context(
            ctx.cfg,
            one_shot,
            ctx.generic_json,
            ctx.pseudo_port_map,
            ctx.functional_output_order,
        )


@pytest.mark.unit
@pytest.mark.parametrize("from_sat", [True, False])
def test_decompressed_pattern_is_its_loads_whole_stream(
    tmp_path: Path, from_sat: bool
) -> None:
    """With the free bits the solver chose (`seed`) or, for a vector replayed from the
    database, solved for from the vector alone: the same kind of pattern."""
    ctx = _context(tmp_path, 5, 2)
    max_len = int(ctx.manifest["max_chain_length"])
    shifts = _shifts(ctx)
    width = 8 * shifts
    free = random.Random(7).getrandbits(width)
    vector = _load_vector(ctx, 8, free)
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    given = _free_bits(free, width) if from_sat else None
    pattern = _decompressed(ctx, vector, loaded, {}, given)

    positions = [(c, t) for c in sorted(loaded.load_seqs) for t in range(max_len)]
    assert pattern.load_care == tuple(positions)
    # The loaded cells keep their values; the cycle that passes through the short
    # chain is the decompressor's too.
    for ppi, (chain, cycle) in _load_positions(ctx).items():
        assert pattern.load_seqs[chain][cycle] == vector[ppi]
    assert pattern.capture_pi_values == loaded.capture_pi_values
    assert pattern.expected_unload == loaded.expected_unload
    # What a tester drives: every shift's word, the warm-up's included.
    assert pattern.channel_seqs is not None
    assert sorted(pattern.channel_seqs) == list(range(8))
    assert all(len(seq) == shifts for seq in pattern.channel_seqs.values())
    if from_sat:
        assert pattern.channel_seqs == {
            k: [bool((free >> (t * 8 + k)) & 1) for t in range(shifts)]
            for k in range(8)
        }
    # The words are the loads: the numeric decompressor, fed them, shifts in what the
    # pattern loads from the warm-up on.
    words = [[pattern.channel_seqs[k][t] for k in range(8)] for t in range(shifts)]
    shifted = continuous_scan_in_bits(
        lookup_polynomial(RING),
        build_phase_shifter(RING, len(loaded.load_seqs)),
        8,
        words,
    )
    for chain, bits in pattern.load_seqs.items():
        assert bits == [shifted[WARMUP + t][chain] for t in range(max_len)]


@pytest.mark.unit
def test_decompressed_solves_again_for_a_seed_that_is_not_the_vectors(
    tmp_path: Path,
) -> None:
    """Free bits that do not make the vector's load (a vector changed since the solve)
    are not trusted: the pattern is still the vector's."""
    ctx = _context(tmp_path, 5, 2)
    width = 8 * _shifts(ctx)
    vector = _load_vector(ctx, 8, random.Random(3).getrandbits(width))
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    wrong = _free_bits(random.Random(4).getrandbits(width), width)
    pattern = _decompressed(ctx, vector, loaded, {}, wrong)
    for ppi, (chain, cycle) in _load_positions(ctx).items():
        assert pattern.load_seqs[chain][cycle] == vector[ppi]


@pytest.mark.unit
def test_decompressed_refuses_a_load_no_free_bits_make(tmp_path: Path) -> None:
    """Forty cells, one channel: some loads are out of reach. A candidate with one
    would be a bug upstream (the solver and random fill draw channel words)."""
    ctx = _context(tmp_path, 40, 2, channels=1)
    rows = _rows(ctx, 1)
    positions = _load_positions(ctx)
    cell_rows = {
        ppi: rows[WARMUP + cycle][chain] for ppi, (chain, cycle) in positions.items()
    }
    dependent = _dependent_cells(cell_rows)
    vector: dict[str, bool] = {"B": False, **{ppi: False for ppi in positions}}
    vector[dependent[0]] = True
    assert not _consistent([(cell_rows[p], vector[p]) for p in positions])
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    with pytest.raises(ScanError, match="channel"):
        _decompressed(ctx, vector, loaded, {})


@pytest.mark.unit
def test_decompressed_launch_shift_bits_come_from_the_same_words(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path, 40, 2, channels=1)
    max_len = int(ctx.manifest["max_chain_length"])
    shifts = _shifts(ctx)
    rows = _rows(ctx, 1)
    free = random.Random(11).getrandbits(shifts)
    vector = _load_vector(ctx, 1, free)
    loaded = serialize_vector(vector, ctx.pseudo_port_map, ctx.manifest)
    heads = {
        chain: _parity(rows[WARMUP + max_len][chain], free)
        for chain in loaded.load_seqs
    }
    pattern = _decompressed(ctx, vector, loaded, heads)
    assert pattern.channel_seqs is not None
    chosen = sum((1 << t) for t in range(shifts) if pattern.channel_seqs[0][t])
    for chain, bit in heads.items():
        assert _parity(rows[WARMUP + max_len][chain], chosen) == bit
    # A launch-shift bit no free bits of this load give is refused.
    load_equations = [
        (rows[WARMUP + cycle][chain], vector[p])
        for p, (chain, cycle) in _load_positions(ctx).items()
    ]
    refused = 0
    for chain in heads:
        flipped = {**heads, chain: not heads[chain]}
        equations = load_equations + [
            (rows[WARMUP + max_len][c], bit) for c, bit in flipped.items()
        ]
        if _consistent(equations):
            _decompressed(ctx, vector, loaded, flipped)
        else:
            refused += 1
            with pytest.raises(ScanError, match="channel"):
                _decompressed(ctx, vector, loaded, flipped)
    assert refused > 0


@pytest.mark.unit
def test_seeded_random_vectors_are_loads_of_one_set_of_words(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Forty cells, one channel: random loads would almost never be a load it can
    make. Each draw is the free bits the cells' values come from, returned with it."""
    from faultflow.runner.runner import _atpg_pi_names, _load_core

    core = _load_core()
    ctx = _context(tmp_path, 40, 3, channels=1)
    max_len = int(ctx.manifest["max_chain_length"])
    shifts = _shifts(ctx)
    rows = _rows(ctx, 1)
    order = _atpg_pi_names(ctx.cfg.intermediate_dir / "scan_atpg_view.json", TOP)
    positions = _load_positions(ctx)

    drawn = _seeded_random_vectors(core, order, ctx, 32, los=True)
    assert len(drawn) == 32
    assert drawn == _seeded_random_vectors(core, order, ctx, 32, los=True)
    assert len({tuple(sorted(v.items())) for v, _, _ in drawn}) > 16
    assert {v["B"] for v, _, _ in drawn} == {False, True}
    for vector, heads, seed in drawn:
        assert set(vector) == set(order)
        assert seed is not None and len(seed) == shifts
        free = sum(1 << t for t, bit in enumerate(seed) if bit)
        for ppi, (chain, cycle) in positions.items():
            assert vector[ppi] == _parity(rows[WARMUP + cycle][chain], free)
        assert set(heads) == set(range(len(ctx.manifest["scan_inputs"])))
        for chain, bit in heads.items():
            assert bit == _parity(rows[WARMUP + max_len][chain], free)
    # Without launch-on-shift there are no launch-shift bits.
    assert all(h == {} for _, h, _ in _seeded_random_vectors(core, order, ctx, 4))
