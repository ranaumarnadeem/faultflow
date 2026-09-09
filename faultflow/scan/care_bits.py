"""Scan care-bit extraction: determine which ``(chain, cycle)`` positions of a
``ScanPattern``'s ``load_seqs`` a SAT-accepted candidate actually needed a
specific value for -- as opposed to don't-care positions ATPG never
constrained.

Mirrors ``faultflow.runner.compaction._extract_cube``'s proven flip/
re-simulate technique (flip a bit to 0, flip to 1, re-simulate both holding
everything else fixed; if targeted faults stay detected under both, it's a
don't-care), indexed by ``(chain_id, cycle)`` instead of named PI, since no
don't-care concept exists anywhere for scan patterns today -- ``load_seqs``
is always fully specified by design (see
``faultflow/scan/detection_pipeline.py``'s load-bearing comment on X->0
filling). Needed before a candidate's care bits can be solved against a
compression decompressor's structure via ``solve_xor_broadcast``.
"""

from __future__ import annotations

from typing import Callable

DetectOracle = Callable[[dict[int, list[bool]], set[int]], set[int]]


def extract_scan_care_bits(
    detect: DetectOracle,
    load_seqs: dict[int, list[bool]],
    max_chain_length: int,
    targets: set[int],
) -> list[tuple[int, int, bool]]:
    """The specified ``(chain_id, cycle, value)`` subset of ``load_seqs``
    that still detects ``targets``.

    A ``(chain, cycle)`` position is a don't-care iff ``targets`` stay
    detected for BOTH its values (every other position held at
    ``load_seqs``'s values); such positions are dropped. Returns specified
    positions only. 2 ``detect()`` calls per position -- callers should scope
    this to sparse, SAT-targeted candidates only (mirrors compaction.py's own
    dense/sparse cost gate), never random-fill patterns (any random seed is
    trivially satisfiable through a compressor -- nothing to extract).
    """
    care: list[tuple[int, int, bool]] = []
    trial: dict[int, list[bool]] = {
        chain_id: list(bits) for chain_id, bits in load_seqs.items()
    }
    for chain_id, bits in load_seqs.items():
        for cycle in range(max_chain_length):
            original = bits[cycle]
            trial[chain_id][cycle] = False
            det_lo = detect(trial, targets)
            trial[chain_id][cycle] = True
            det_hi = detect(trial, targets)
            trial[chain_id][cycle] = original
            if targets.issubset(det_lo) and targets.issubset(det_hi):
                continue  # don't-care
            care.append((chain_id, cycle, original))
    return care
