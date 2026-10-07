"""Sequential ring-generator (LFSR) + phase-shifter model for scan-pattern
compression.

Single source of truth for the LFSR's structure, shared by BOTH the RTL
generator (``compression.py::ring_generator_wrapper_verilog``) and the GF(2)
seed-solving math (``care_bit_rows``, consumed by the ``solve_xor_broadcast``
binding) -- so the synthesized hardware and the Python model used to solve for
compressible seeds can never drift out of sync. See the compression plan for
the full architecture.

This models the decades-old, never-patented academic mechanism: an
internal-XOR (Galois-form) LFSR ("ring generator") whose state evolves once
per shift clock, decorrelated across multiple scan chains by a static XOR
"phase shifter" -- Bardell, "Design Considerations for Parallel Pseudorandom
Pattern Generators," JETTA vol.1 no.1, 1990 (the phase-shifter concept);
Koenemann, "LFSR-Coded Test Patterns for Scan Designs," ETC 1991 (LFSR
reseeding); Rajski/Tyszer/Zacharia, "Test Data Decompression for Multiple Scan
Designs with Boundary Scan," IEEE Trans. Computers, Nov 1998 (the combined
architecture, published over a year before the first EDT patent's priority
date). Not implemented from, or modeled on, any patent's claim language.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class LfsrPolynomial:
    """A Galois-form (internal-XOR) LFSR feedback structure.

    ``taps`` are stage indices in ``{1, ..., width-1}`` that get an extra
    feedback XOR gate on their input (stage 0 always receives the raw feedback
    bit with no XOR -- see ``_step``). The feedback bit is always
    ``state[width-1]``.
    """

    width: int
    taps: frozenset[int]


# Tap sets are the internal (Galois) positions of well-known, decades-old,
# public-domain maximal-length LFSR feedback polynomials (the same family of
# tables long republished in FPGA/ASIC application notes, e.g. Xilinx XAPP 052,
# and equivalent to Bardell/McAnney/Savir 1987's own primitive-polynomial
# appendix) -- copied, not invented, and never sourced from any patent.
# Maximal-length (period 2^width - 1) is exhaustively verified for width 8/16
# in tests/python/scan/test_ring_generator.py; width 32/64 get a bounded
# non-degeneracy sanity check only (exhaustive verification is infeasible at
# that state-space size).
PRIMITIVE_POLYNOMIALS: dict[int, LfsrPolynomial] = {
    8: LfsrPolynomial(8, frozenset({4, 5, 6})),  # x^8 + x^6 + x^5 + x^4 + 1
    16: LfsrPolynomial(16, frozenset({4, 13, 15})),  # x^16 + x^15 + x^13 + x^4 + 1
    32: LfsrPolynomial(32, frozenset({1, 2, 22})),  # x^32 + x^22 + x^2 + x^1 + 1
    64: LfsrPolynomial(64, frozenset({60, 61, 63})),  # x^64 + x^63 + x^61 + x^60 + 1
}


def lookup_polynomial(width: int) -> LfsrPolynomial:
    """Return the curated maximal-length polynomial for ``width``.

    Scope is deliberately bounded to exactly the tabulated widths above --
    extending to an arbitrary width needs sourcing another verified
    primitive-polynomial entry, not generating one ad hoc.
    """
    if width not in PRIMITIVE_POLYNOMIALS:
        raise ValueError(
            f"no curated primitive-polynomial tap set for width {width}; "
            f"supported widths: {sorted(PRIMITIVE_POLYNOMIALS)}"
        )
    return PRIMITIVE_POLYNOMIALS[width]


def tap_mask_verilog_literal(poly: LfsrPolynomial) -> str:
    """A ``<width>'b<bits>`` Verilog literal, bit ``i`` set iff ``i in poly.taps``.

    The RTL generator renders feedback taps ONLY through this function -- never
    by hand-writing tap bits -- so the emitted hardware can't silently drift
    from ``poly.taps``. See the round-trip test in
    ``tests/python/scan/test_ring_generator.py``.
    """
    bits = "".join("1" if i in poly.taps else "0" for i in reversed(range(poly.width)))
    return f"{poly.width}'b{bits}"


def _step(rows: list[int], poly: LfsrPolynomial) -> list[int]:
    """Advance one shift cycle.

    Works identically whether ``rows[i]`` holds a raw 0/1 bit (real LFSR
    state) or a GF(2) coefficient bitmask over seed bits (symbolic row, for
    ``care_bit_rows``) -- the XOR recurrence is linear, so both uses share this
    one function; no separate matrix-power machinery is needed.
    """
    fb = rows[poly.width - 1]
    new = [0] * poly.width
    new[0] = fb
    for i in range(1, poly.width):
        new[i] = rows[i - 1] ^ (fb if i in poly.taps else 0)
    return new


def care_bit_rows(
    poly: LfsrPolynomial, phase_shifter_taps: list[list[int]], max_chain_length: int
) -> list[list[int]]:
    """``out[t][c]`` = GF(2) coefficient bitmask (over the K seed/channel bits)
    for scan chain ``c``'s ``scan_in`` value at shift cycle ``t``.

    ``rows[i]`` starts as ``1 << i`` (state(0) == seed, identity map) and is
    advanced via ``_step`` once per cycle; each cycle's phase-shifter output
    for chain ``c`` is the XOR of ``rows[k]`` for every register bit ``k`` in
    ``phase_shifter_taps[c]``. This row becomes one ``CareBit``'s effective
    coefficient row for ``solve_xor_broadcast`` -- reused completely
    unmodified (see ``src/core/scan/compression.{hpp,cpp}``).
    """
    rows = [1 << i for i in range(poly.width)]
    out: list[list[int]] = []
    for _ in range(max_chain_length):
        out.append([_xor_reduce(rows[k] for k in taps) for taps in phase_shifter_taps])
        rows = _step(rows, poly)
    return out


def _xor_reduce(values: Iterable[int]) -> int:
    acc = 0
    for v in values:
        acc ^= v
    return acc


# The continuous-flow decompressor: the ring generator is cleared on the first shift
# of a load (the scan enable's rise, after a clock with it low) and takes the channel
# word applied at every shift into its injector stages, so a pattern has
# channels x shifts free bits instead of one seed's (Rajski/Tyszer/Zacharia's
# continuous decompression, 1998, cited above). Shift t's scan-ins read the ring's
# state after shift t-1's injection: shift 0's are the cleared ring's, 0.


def injector_stages(width: int, num_channels: int) -> list[int]:
    """The ring stage each channel bit XORs into on every shift: channel c into stage
    c*width//num_channels, spread evenly so that a fresh channel bit reaches every
    stage within ceil(width/num_channels) shifts."""
    if not 1 <= num_channels <= width:
        raise ValueError(
            f"{num_channels} channels into a {width}-stage ring: need 1 to {width}"
        )
    return [(c * width) // num_channels for c in range(num_channels)]


def default_ring_width(num_channels: int) -> int:
    """The smallest curated ring width at least twice the channels, or the widest one:
    a ring wider than its channels carries their bits across shifts, so a shift with
    many care bits can use bits applied earlier."""
    widths = sorted(PRIMITIVE_POLYNOMIALS)
    if not 1 <= num_channels <= widths[-1]:
        raise ValueError(f"channels must be 1 to {widths[-1]}, got {num_channels}")
    return next((w for w in widths if w >= 2 * num_channels), widths[-1])


def default_warmup(width: int, num_channels: int) -> int:
    """Shifts before the first that loads a scan cell: enough for a channel bit to
    reach every ring stage, and at least one, since the clearing shift loads 0."""
    return max(1, -(-width // num_channels))


def continuous_ring_states(
    poly: LfsrPolynomial, num_channels: int, cycles: int
) -> list[list[int]]:
    """``states[t][k]``: ring stage k at shift t (the state its scan-ins read), as a
    GF(2) bitmask over the channel bits -- bit t*num_channels + c is channel c of the
    word applied at shift t. ``states[0]`` is the cleared ring."""
    injectors = injector_stages(poly.width, num_channels)
    state = [0] * poly.width
    states: list[list[int]] = []
    for t in range(cycles):
        states.append(state)
        state = _step(state, poly)
        for c, stage in enumerate(injectors):
            state[stage] ^= 1 << (t * num_channels + c)
    return states


def continuous_scan_in_rows(
    poly: LfsrPolynomial,
    phase_shifter_taps: list[list[int]],
    num_channels: int,
    cycles: int,
) -> list[list[int]]:
    """``rows[t][c]``: scan chain c's scan-in at shift t of a load, for t in
    range(cycles), as a GF(2) bitmask over the channel bits (bit t*num_channels + k:
    channel k of the word applied at shift t; see continuous_ring_states). A cell at
    position p of a chain loads the scan-in of shift S-1-p, S the load's shifts;
    launching on shift, the chain's head takes shift S's."""
    return [
        [_xor_reduce(state[k] for k in taps) for taps in phase_shifter_taps]
        for state in continuous_ring_states(poly, num_channels, cycles)
    ]


def continuous_scan_in_bits(
    poly: LfsrPolynomial,
    phase_shifter_taps: list[list[int]],
    num_channels: int,
    channel_words: list[list[bool]],
) -> list[list[bool]]:
    """The scan-ins a load shifts in when the channels take ``channel_words[t]`` at
    shift t: one row per shift, chain by chain. The numeric reference for
    continuous_scan_in_rows."""
    injectors = injector_stages(poly.width, num_channels)
    state = [0] * poly.width
    out: list[list[bool]] = []
    for word in channel_words:
        if len(word) != num_channels:
            raise ValueError(f"a channel word has {len(word)} bits, not {num_channels}")
        out.append(
            [bool(_xor_reduce(state[k] for k in taps)) for taps in phase_shifter_taps]
        )
        state = _step(state, poly)
        for c, stage in enumerate(injectors):
            state[stage] ^= int(word[c])
    return out


def bitmask_to_index_list(mask: int) -> list[int]:
    """Convert a GF(2) coefficient bitmask (as produced by ``care_bit_rows``)
    into the sorted list-of-indices ``fanout`` row shape
    ``solve_xor_broadcast`` (``src/core/scan/compression.hpp``) expects."""
    indices = []
    i = 0
    while mask:
        if mask & 1:
            indices.append(i)
        mask >>= 1
        i += 1
    return indices
