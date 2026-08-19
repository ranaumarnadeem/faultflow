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


def _strip_block_level_padding(
    seq: list[bool], real_length: int, *, pad_at_front: bool
) -> list[bool]:
    """A block's own exported `load_seqs`/`expected_unload` are already padded to
    THAT block's own manifest max_chain_length (`serialize_vector`,
    faultflow/scan/protocol.py) whenever the block itself has chains of differing
    lengths (e.g. genericfir_small: 39 chains of length 10, 9 of length 9, all
    exported padded to the block's own max_chain_length=10). `_block_value_at`
    expects an UNPADDED sequence of exactly `real_length` elements, so any such
    pre-existing padding must be stripped before it's called, mirroring
    serialize_vector's own convention: load_seqs pads BEFORE the real payload,
    expected_unload/unload pads AFTER. A block chain that already equals that
    block's own max_chain_length has zero padding, so this is a no-op there
    (which is why iiravg/boxcar's uniform-length chains never exposed this)."""
    if len(seq) <= real_length:
        return seq
    return seq[len(seq) - real_length :] if pad_at_front else seq[:real_length]


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
            bload = _strip_block_level_padding(
                list(bp_load.get(bc, [])), seg.length, pad_at_front=True
            )
            bunload = _strip_block_level_padding(
                list(bp_unload.get(bc, [])), seg.length, pad_at_front=False
            )
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
        # pad up to the SoC max chain length -- LOAD and UNLOAD pad on OPPOSITE
        # ends, because a chain shorter than max_chain_length is driven under the
        # same shared cycle count as the longest declared chain (one shift-enable,
        # simulate_scan_pattern always runs exactly max_chain_length shift-in
        # cycles for every scan_input_port). A physical register only holds its
        # LAST `length` fed bits -- anything shifted in earlier is pushed out
        # through scan_out before the capture edge. So on LOAD, padding must come
        # BEFORE the real payload (the real bits are fed during the FINAL `length`
        # cycles and are what the register still holds at capture time); appending
        # padding after the payload -- as this used to do -- shifts the intended
        # bits straight out and leaves the register holding the padding (all
        # False) instead, silently corrupting every combined-chain retarget with
        # pad > 0 while returning as normal for the single-chain (pad == 0) case.
        # UNLOAD is the mirror: the real `length` bits drain out during the FIRST
        # `length` unload cycles, so expected_unload/unload_mask correctly keep
        # padding AFTER (unaffected by this fix).
        pad = soc_access.max_chain_length - length
        if pad < 0:
            raise SocAccessError(
                f"SoC chain {chain.index} length {length} exceeds max_chain_length"
            )
        load_seqs[chain.index] = [False] * pad + [
            load_v[length - 1 - k] for k in range(length)
        ]
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
