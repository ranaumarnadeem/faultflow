from __future__ import annotations

# Chain bit-ordering for load/unload sequences.
#
# stitch.py wires: SI -> FF[chain_position=0].SDI -> ... -> FF[N-1].Q -> SO
# (see ScanCellRecord.chain_position and previous_q chaining in _build_plan).
#
# golden_ref_sim.cpp: on posedge with SE active, each FF captures its SDI net.
# First SI feed lands in position 0, then propagates toward SO. To leave a
# requested state in the chain, the highest chain position is fed first.
#
# Validated against tests/cpp/scan/test_scan_chain.cpp and tiny_scan_multichain.json
# via tests/python/test_scan_protocol_constants.py.

LOAD_BIT_ORDER = "descending_position"
UNLOAD_BIT_ORDER = "descending_position"


def load_sequence(target_by_position: dict[int, bool], chain_length: int) -> list[bool]:
    """Bits fed at SI to reach target_by_position after chain_length shifts."""
    if chain_length < 0:
        raise ValueError("chain_length must be >= 0")
    return [
        target_by_position.get(position, False)
        for position in range(chain_length - 1, -1, -1)
    ]


def unload_sequence(
    target_by_position: dict[int, bool], chain_length: int
) -> list[bool]:
    """Bits observed at SO when unloading chain_length shifts after capture."""
    if chain_length < 0:
        raise ValueError("chain_length must be >= 0")
    return [
        target_by_position.get(position, False)
        for position in range(chain_length - 1, -1, -1)
    ]
