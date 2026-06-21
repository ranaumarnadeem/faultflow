"""SoC scan-pattern retargeting (Stage 5).

A block is tested in isolation (INTEST over its wrapper boundary + internal scan),
producing per-chain load/capture/unload sequences. Retargeting maps those onto the
SoC-level scan chains — by daisy-chain concatenation + BYPASS fill for the sibling
blocks — so the block's coverage is delivered from chip pins with no re-ATPG at the
top, then proven by replaying the retargeted sequence on the SoC netlist
(`verify_soc`) and comparing the block's unload at its SoC offsets.
"""

from __future__ import annotations

from faultflow.retarget.soc_access import (
    SocAccess,
    SocChain,
    SocSegment,
    load_soc_access,
)
from faultflow.retarget.transform import SocScanPattern, retarget_block_pattern

__all__ = [
    "SocAccess",
    "SocChain",
    "SocSegment",
    "load_soc_access",
    "SocScanPattern",
    "retarget_block_pattern",
]
