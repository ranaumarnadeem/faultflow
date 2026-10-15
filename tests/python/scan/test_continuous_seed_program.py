"""The continuous decompressor's loads as a straight-line program over its free bits
(``ring_generator.continuous_seed_program``): variable ``seed_width + j`` is the XOR of
earlier variables, so a scan cell's value is a short XOR of ring-stage variables however
many shifts the load takes, where the flat row of ``continuous_scan_in_rows`` grows with
them. The SAT solves take it as ``seed_defs`` (``test_seeded_scan_atpg.cpp``).

Every property is checked against the model the RTL is checked against: the numeric ring
(``continuous_scan_in_bits``) and the flat rows (``continuous_scan_in_rows``)."""

from __future__ import annotations

import random

import pytest

from faultflow.scan.compression import build_phase_shifter
from faultflow.scan.ring_generator import (
    PRIMITIVE_POLYNOMIALS,
    continuous_scan_in_bits,
    continuous_scan_in_rows,
    continuous_seed_program,
)

CASES = [(8, 1, 6), (8, 3, 9), (16, 4, 12), (16, 8, 20), (32, 5, 10)]


def _values(program, free):
    values = list(free)
    for definition in program.defs:
        values.append(sum(values[i] for i in definition) & 1)
    return values


@pytest.mark.unit
@pytest.mark.parametrize(("width", "channels", "shifts"), CASES)
def test_the_programs_scan_ins_are_the_numeric_rings(width, channels, shifts):
    poly = PRIMITIVE_POLYNOMIALS[width]
    taps = build_phase_shifter(width, 5)
    program = continuous_seed_program(poly, channels, shifts)
    assert program.seed_width == channels * shifts
    rng = random.Random(width * 100 + channels)
    for _ in range(4):
        # One word past the load: the launch-on-shift shift's scan-ins read the ring
        # after the load's last word, not a word of their own.
        words = [
            [rng.randint(0, 1) for _ in range(channels)] for _ in range(shifts + 1)
        ]
        free = [words[t][c] for t in range(shifts) for c in range(channels)]
        values = _values(program, free)
        expected = continuous_scan_in_bits(
            poly, taps, channels, [[bool(b) for b in word] for word in words]
        )
        for t in range(shifts + 1):
            for chain, chain_taps in enumerate(taps):
                got = sum(values[i] for i in program.scan_in(t, chain_taps)) & 1
                assert bool(got) == expected[t][chain], (t, chain)


@pytest.mark.unit
@pytest.mark.parametrize(("width", "channels", "shifts"), CASES)
def test_the_programs_expansion_is_the_flat_row(width, channels, shifts):
    poly = PRIMITIVE_POLYNOMIALS[width]
    taps = build_phase_shifter(width, 4)
    program = continuous_seed_program(poly, channels, shifts)
    masks = [1 << i for i in range(program.seed_width)]
    for definition in program.defs:
        mask = 0
        for i in definition:
            mask ^= masks[i]
        masks.append(mask)
    rows = continuous_scan_in_rows(poly, taps, channels, shifts + 1)
    for t in range(shifts + 1):
        for chain, chain_taps in enumerate(taps):
            mask = 0
            for i in program.scan_in(t, chain_taps):
                mask ^= masks[i]
            assert mask == rows[t][chain], (t, chain)


@pytest.mark.unit
def test_a_definition_reads_only_earlier_variables_and_the_ring_starts_cleared():
    poly = PRIMITIVE_POLYNOMIALS[16]
    program = continuous_seed_program(poly, 4, 15)
    for j, definition in enumerate(program.defs):
        assert definition == sorted(set(definition))
        assert len(definition) >= 2  # one term is the variable itself, none is a zero
        assert all(0 <= i < program.seed_width + j for i in definition)
    taps = build_phase_shifter(16, 3)
    assert all(program.scan_in(0, row) == [] for row in taps)


@pytest.mark.unit
def test_the_program_grows_with_the_load_not_with_the_cells_it_feeds():
    """At 8 channels into a 16-stage ring, 200 shifts: a flat row per scan-in is up to
    1600 variables wide; the program is at most one definition per ring stage per shift.
    """
    poly = PRIMITIVE_POLYNOMIALS[16]
    program = continuous_seed_program(poly, 8, 200)
    assert len(program.defs) <= 16 * 200
    assert max(len(d) for d in program.defs) <= 3
    taps = build_phase_shifter(16, 40)
    assert max(len(program.scan_in(t, row)) for t in (50, 150) for row in taps) <= 3
