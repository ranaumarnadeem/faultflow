"""The continuous-flow decompressor model (faultflow.scan.ring_generator): the ring
generator cleared by the scan-enable rise and fed the channel bits on every shift,
read through the phase shifter (faultflow.scan.compression.build_phase_shifter)."""

from __future__ import annotations

import random

import pytest

from faultflow.scan.compression import build_phase_shifter
from faultflow.scan.ring_generator import (
    PRIMITIVE_POLYNOMIALS,
    continuous_ring_states,
    continuous_scan_in_bits,
    continuous_scan_in_rows,
    default_ring_width,
    default_warmup,
    injector_stages,
    lookup_polynomial,
)


def _parity(row: int, bits: int) -> bool:
    return bin(row & bits).count("1") % 2 == 1


def _rank(rows: list[int]) -> int:
    basis: dict[int, int] = {}
    for row in rows:
        while row:
            top = row.bit_length() - 1
            if top not in basis:
                basis[top] = row
                break
            row ^= basis[top]
    return len(basis)


CONFIGS = [(8, 1, 3), (8, 3, 5), (16, 8, 16), (16, 2, 4), (32, 4, 7)]


def test_injectors_spread_the_channels_over_the_ring() -> None:
    assert injector_stages(16, 8) == [0, 2, 4, 6, 8, 10, 12, 14]
    assert injector_stages(8, 3) == [0, 2, 5]
    assert injector_stages(8, 8) == list(range(8))
    assert injector_stages(8, 1) == [0]
    for channels in (0, 9):
        with pytest.raises(ValueError):
            injector_stages(8, channels)


def test_default_ring_width_is_the_smallest_curated_one_twice_the_channels() -> None:
    assert [default_ring_width(c) for c in (1, 4, 5, 8, 16, 17, 32, 33, 64)] == [
        8,
        8,
        16,
        16,
        32,
        64,
        64,
        64,
        64,
    ]
    for channels in (0, 65):
        with pytest.raises(ValueError):
            default_ring_width(channels)


def test_default_warmup_lets_every_stage_take_a_channel_bit() -> None:
    assert default_warmup(16, 8) == 2
    assert default_warmup(8, 1) == 8
    assert default_warmup(8, 3) == 3
    assert default_warmup(64, 64) == 1


@pytest.mark.parametrize("width,channels,chains", CONFIGS)
def test_the_clearing_shift_loads_zero(width: int, channels: int, chains: int) -> None:
    poly = lookup_polynomial(width)
    rows = continuous_scan_in_rows(
        poly, build_phase_shifter(width, chains), channels, 12
    )
    assert rows[0] == [0] * chains


@pytest.mark.parametrize("width,channels,chains", CONFIGS)
def test_a_shifts_channel_word_reaches_only_later_shifts(
    width: int, channels: int, chains: int
) -> None:
    poly = lookup_polynomial(width)
    cycles = 12
    rows = continuous_scan_in_rows(
        poly, build_phase_shifter(width, chains), channels, cycles
    )
    for t in range(cycles):
        word = ((1 << channels) - 1) << (t * channels)
        assert all(row & word == 0 for shift in rows[: t + 1] for row in shift), t
    assert all(row < 1 << (channels * (cycles - 1)) for shift in rows for row in shift)


@pytest.mark.parametrize("width,channels,chains", CONFIGS)
def test_the_rows_give_what_the_numeric_model_shifts_in(
    width: int, channels: int, chains: int
) -> None:
    poly = lookup_polynomial(width)
    taps = build_phase_shifter(width, chains)
    cycles = 14
    rows = continuous_scan_in_rows(poly, taps, channels, cycles)
    rng = random.Random(width * 100 + channels)
    for _ in range(20):
        words = [[rng.random() < 0.5 for _ in range(channels)] for _ in range(cycles)]
        bits = sum(
            1 << (t * channels + c)
            for t in range(cycles)
            for c in range(channels)
            if words[t][c]
        )
        assert continuous_scan_in_bits(poly, taps, channels, words) == [
            [_parity(row, bits) for row in rows[t]] for t in range(cycles)
        ]


@pytest.mark.parametrize("width", sorted(PRIMITIVE_POLYNOMIALS))
@pytest.mark.parametrize("channels", [1, 3, 8])
def test_after_the_warmup_every_ring_stage_holds_a_channel_bit(
    width: int, channels: int
) -> None:
    poly = lookup_polynomial(width)
    warmup = default_warmup(width, channels)
    states = continuous_ring_states(poly, channels, warmup + 1)
    assert states[0] == [0] * width
    assert _rank(states[warmup]) == width


def test_the_phase_shifter_gives_each_chain_its_own_three_taps() -> None:
    for width, chains in ((8, 3), (8, 16), (16, 16), (16, 5), (32, 40), (64, 7)):
        rows = build_phase_shifter(width, chains)
        assert len(rows) == chains
        assert all(len(set(row)) == 3 and row == sorted(row) for row in rows)
        assert all(0 <= tap < width for row in rows for tap in row)
        assert len({tuple(row) for row in rows}) == chains
        assert build_phase_shifter(width, chains) == rows


def test_the_phase_shifter_reads_every_stage_when_it_has_the_chains() -> None:
    for width, chains in ((8, 8), (8, 16), (16, 16), (32, 40)):
        rows = build_phase_shifter(width, chains)
        assert {tap for row in rows for tap in row} == set(range(width))


def test_no_chain_reads_a_rotation_of_another_chains_taps_while_the_ring_allows() -> (
    None
):
    """A tap set rotated by d is (but for the feedback) the same scan-in d shifts
    later, so its chain's cells would copy another chain's."""
    for width, chains in ((8, 5), (16, 16), (32, 32), (64, 64)):
        rows = [frozenset(row) for row in build_phase_shifter(width, chains)]
        for i, row in enumerate(rows):
            for other in rows[:i]:
                assert all(
                    frozenset((tap + d) % width for tap in other) != row
                    for d in range(width)
                ), (width, chains)
