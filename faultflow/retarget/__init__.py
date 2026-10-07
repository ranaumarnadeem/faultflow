"""A block's INTEST scan patterns retargeted onto the SoC it sits in.

Each block of a ``faultflow_project_v2`` SoC is tested on its own, through its IEEE
1500 wrapper (ff.py project). Its chains are pieces of the SoC's chains, so its
patterns become the SoC's with no ATPG at the top: each block chain's load and
expected unload placed where the block chain sits on the SoC, the block's inputs
set through the SoC's, and every block held in INTEST (transform).
"""

from __future__ import annotations

from faultflow.retarget.transform import (
    RetargetError,
    Segment,
    block_inputs,
    block_segments,
    retarget_pattern,
    retarget_patterns,
)

__all__ = [
    "RetargetError",
    "Segment",
    "block_inputs",
    "block_segments",
    "retarget_pattern",
    "retarget_patterns",
]
