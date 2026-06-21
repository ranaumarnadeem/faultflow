"""Retarget a block-level scan pattern onto the SoC-level scan chains.

Geometry (the part that is easy to get wrong, so it is stated explicitly): a scan
chain shifts ``scan_in -> position 0 -> ... -> position L-1 -> scan_out``. Loading
feeds ``scan_in`` for ``L`` cycles, so the LAST-fed bit lands at position 0 and the
first-fed at position ``L-1`` — i.e. the shift-order sequence ``s`` and the
position-indexed values ``V`` are reverses: ``s[k] == V[L-1-k]``. Unload is the same
on the way out.

A block chain embedded as a SoC segment at ``soc_offset = o`` puts block position
``p`` at SoC position ``o + p`` (the segment keeps its orientation: its head, nearest
its own scan-in, sits nearest the SoC scan-in). So the retarget is: decode the block
sequences to block-position values, place them at ``o + p``, fill every other SoC
position with BYPASS (don't-care), then re-encode to SoC shift order. The BYPASS /
sibling positions are masked out of the unload comparison (``unload_mask``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from faultflow.retarget.soc_access import SocAccess, SocAccessError


@dataclass(frozen=True)
class SocScanPattern:
    """A retargeted pattern in SoC scan-chain space (mirrors scan.ScanPattern, plus
    a per-chain ``unload_mask`` marking the positions owned by ``source_block`` —
    the only positions whose unload is meaningful; sibling/BYPASS bits are masked)."""

    load_seqs: dict[int, list[bool]]
    capture_pi_values: dict[str, bool]
    expected_unload: dict[int, list[bool]]
    unload_mask: dict[int, list[bool]]
    source_block: str
    block_chains: tuple[int, ...] = field(default_factory=tuple)


def _block_value_at(seq: list[bool], length: int, block_pos: int) -> bool:
    """Value at block chain position `block_pos` (0 = head), given a descending-
    shift-order sequence `seq` of a chain of `length`. ``seq[k] == V[length-1-k]``."""
    shift_idx = length - 1 - block_pos
    return bool(seq[shift_idx]) if 0 <= shift_idx < len(seq) else False


def retarget_block_pattern(
    block_pattern: Any,
    soc_access: SocAccess,
    source_block: str,
) -> SocScanPattern:
    """Place `source_block`'s per-chain load/unload at its SoC segment offsets, with
    BYPASS fill for every sibling/non-INTEST position. `block_pattern` is any object
    with ``load_seqs``/``capture_pi_values``/``expected_unload`` (e.g. a
    ``faultflow.scan.protocol.ScanPattern``)."""
    bp_load = dict(getattr(block_pattern, "load_seqs", {}))
    bp_unload = dict(getattr(block_pattern, "expected_unload", {}))

    load_seqs: dict[int, list[bool]] = {}
    expected_unload: dict[int, list[bool]] = {}
    unload_mask: dict[int, list[bool]] = {}
    placed_block_chains: set[int] = set()

    for chain in soc_access.soc_chains:
        length = chain.length
        load_v = [False] * length  # position-indexed
        unload_v = [False] * length
        mask_v = [False] * length
        for seg in chain.segments:
            if seg.block != source_block or seg.block_chain is None:
                continue  # sibling block or BYPASS fill -> leave don't-care/masked
            bc = seg.block_chain
            placed_block_chains.add(bc)
            bload = list(bp_load.get(bc, []))
            bunload = list(bp_unload.get(bc, []))
            for p in range(seg.length):
                soc_pos = seg.soc_offset + p
                if soc_pos >= length:
                    raise SocAccessError(
                        f"segment for {source_block!r} chain {bc} overruns SoC chain "
                        f"{chain.index} (pos {soc_pos} >= {length})"
                    )
                load_v[soc_pos] = _block_value_at(bload, seg.length, p)
                unload_v[soc_pos] = _block_value_at(bunload, seg.length, p)
                mask_v[soc_pos] = True

        # Re-encode position-indexed values to descending-position shift order, then
        # pad (after) up to the SoC max chain length, mirroring serialize_vector.
        pad = soc_access.max_chain_length - length
        if pad < 0:
            raise SocAccessError(
                f"SoC chain {chain.index} length {length} exceeds max_chain_length"
            )
        load_seqs[chain.index] = [load_v[length - 1 - k] for k in range(length)] + [
            False
        ] * pad
        expected_unload[chain.index] = [
            unload_v[length - 1 - k] for k in range(length)
        ] + [False] * pad
        unload_mask[chain.index] = [mask_v[length - 1 - k] for k in range(length)] + [
            False
        ] * pad

    missing = set(bp_load) - placed_block_chains
    if missing:
        raise SocAccessError(
            f"block {source_block!r} chains {sorted(missing)} have no SoC segment "
            "in the access manifest"
        )

    return SocScanPattern(
        load_seqs=load_seqs,
        capture_pi_values=dict(getattr(block_pattern, "capture_pi_values", {})),
        expected_unload=expected_unload,
        unload_mask=unload_mask,
        source_block=source_block,
        block_chains=tuple(sorted(placed_block_chains)),
    )
