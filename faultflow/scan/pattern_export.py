"""JSON-safe serialization for scan patterns.

A :class:`~faultflow.scan.protocol.ScanPattern` keys its per-chain sequences by
integer chain id, but JSON object keys are always strings. These helpers convert
between the in-memory pattern and a plain JSON-serializable dict, stringifying
chain ids on the way out and restoring them on the way in. They are the bridge
between block-level ATPG (which produces ``ScanPattern`` objects) and the
SoC-level retarget step (which reads them back from disk).
"""

from __future__ import annotations

from typing import Any

from faultflow.scan.protocol import ScanPattern


def scan_pattern_to_dict(pattern: ScanPattern) -> dict[str, Any]:
    """Convert a ScanPattern to a JSON-serializable dict (int keys -> str)."""
    return {
        "load_seqs": {
            str(chain): [bool(b) for b in seq]
            for chain, seq in pattern.load_seqs.items()
        },
        "capture_pi_values": {
            str(port): bool(value) for port, value in pattern.capture_pi_values.items()
        },
        "expected_unload": {
            str(chain): [bool(b) for b in seq]
            for chain, seq in pattern.expected_unload.items()
        },
    }


def scan_pattern_from_dict(data: dict[str, Any]) -> ScanPattern:
    """Restore a ScanPattern from :func:`scan_pattern_to_dict` output (str -> int)."""

    def _int_keyed(raw: dict[str, Any]) -> dict[int, list[bool]]:
        return {int(chain): [bool(b) for b in seq] for chain, seq in raw.items()}

    return ScanPattern(
        load_seqs=_int_keyed(data["load_seqs"]),
        capture_pi_values={
            str(port): bool(value) for port, value in data["capture_pi_values"].items()
        },
        expected_unload=_int_keyed(data["expected_unload"]),
    )
