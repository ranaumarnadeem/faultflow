from __future__ import annotations

import pytest

from faultflow.scan.ring_generator import (
    PRIMITIVE_POLYNOMIALS,
    _step,
    bitmask_to_index_list,
    care_bit_rows,
    lookup_polynomial,
    tap_mask_verilog_literal,
)


@pytest.mark.unit
@pytest.mark.parametrize("width", [8, 16, 32, 64])
def test_lookup_polynomial_curated_widths(width: int) -> None:
    poly = lookup_polynomial(width)
    assert poly.width == width
    assert poly.taps
    assert all(1 <= t < width for t in poly.taps)


@pytest.mark.unit
def test_lookup_polynomial_rejects_uncurated_width() -> None:
    with pytest.raises(ValueError, match="no curated"):
        lookup_polynomial(24)


@pytest.mark.unit
def test_tap_mask_verilog_literal_round_trips() -> None:
    for width, poly in PRIMITIVE_POLYNOMIALS.items():
        literal = tap_mask_verilog_literal(poly)
        prefix, bits = literal.split("'b")
        assert int(prefix) == width
        assert len(bits) == width
        recovered = frozenset(i for i, ch in enumerate(reversed(bits)) if ch == "1")
        assert recovered == poly.taps


def _run_lfsr(poly, seed, steps):
    rows = list(seed)
    seen = {tuple(rows)}
    for i in range(1, steps + 1):
        rows = _step(rows, poly)
        yield i, rows, tuple(rows) in seen
        seen.add(tuple(rows))


@pytest.mark.unit
def test_step_is_maximal_length_width_8() -> None:
    poly = lookup_polynomial(8)
    seed = [1] + [0] * 7
    period = None
    for i, rows, repeated in _run_lfsr(poly, seed, (1 << 8) + 1):
        if tuple(rows) == tuple(seed):
            period = i
            break
        assert not repeated, f"short cycle at step {i}"
        assert any(rows), "LFSR locked to all-zero state"
    assert period == (1 << 8) - 1


@pytest.mark.unit
def test_step_is_maximal_length_width_16() -> None:
    poly = lookup_polynomial(16)
    seed = [1] + [0] * 15
    period = None
    for i, rows, repeated in _run_lfsr(poly, seed, (1 << 16) + 1):
        if tuple(rows) == tuple(seed):
            period = i
            break
        assert not repeated, f"short cycle at step {i}"
        assert any(rows), "LFSR locked to all-zero state"
    assert period == (1 << 16) - 1


@pytest.mark.unit
@pytest.mark.parametrize("width", [32, 64])
def test_step_does_not_degenerate_for_wide_widths(width: int) -> None:
    """Full-period verification is infeasible for K=32/64 (2^K states) -- this is
    a bounded sanity check (no early repeat, no zero-lock) over a much smaller
    window, not a maximal-length proof. Widths 8/16 above ARE exhaustively
    verified against the true 2^K-1 period."""
    poly = lookup_polynomial(width)
    seed = [1] + [0] * (width - 1)
    for i, rows, repeated in _run_lfsr(poly, seed, 50_000):
        assert not repeated, f"short cycle at step {i}"
        assert any(rows), "LFSR locked to all-zero state"


@pytest.mark.unit
def test_care_bit_rows_shape_and_seed_identity() -> None:
    """At cycle 0 the LFSR state equals the seed identically (no clock edge has
    occurred yet), so with an identity phase shifter, row 0 must be the
    identity map (row 0's entry for chain i has exactly bit i set)."""
    poly = lookup_polynomial(8)
    phase_shifter_taps = [[i] for i in range(8)]
    rows = care_bit_rows(poly, phase_shifter_taps, max_chain_length=5)
    assert len(rows) == 5
    assert len(rows[0]) == 8
    for i in range(8):
        assert rows[0][i] == (1 << i)


@pytest.mark.unit
def test_bitmask_to_index_list() -> None:
    assert bitmask_to_index_list(0) == []
    assert bitmask_to_index_list(1) == [0]
    assert bitmask_to_index_list(0b1010) == [1, 3]
    assert bitmask_to_index_list(0b11111111) == [0, 1, 2, 3, 4, 5, 6, 7]


@pytest.mark.unit
def test_care_bit_rows_matches_manual_xor_reduction() -> None:
    """care_bit_rows[t][c] must equal the XOR (bitwise-XOR of the coefficient
    masks) of the LFSR's per-register-bit rows at cycle t, restricted to
    phase_shifter_taps[c] -- cross-checks the XOR composition independently of
    _step's own internals."""
    poly = lookup_polynomial(8)
    phase_shifter_taps = [[0, 3], [1, 2, 7]]
    max_len = 4
    rows_table = care_bit_rows(poly, phase_shifter_taps, max_len)

    rows = [1 << i for i in range(8)]
    for t in range(max_len):
        for c, taps in enumerate(phase_shifter_taps):
            expected = 0
            for tap in taps:
                expected ^= rows[tap]
            assert rows_table[t][c] == expected
        rows = _step(rows, poly)
