"""Round-trip tests for scan-pattern JSON export/import.

A ScanPattern carries integer chain ids (load_seqs / expected_unload) and string
port names (capture_pi_values). JSON object keys are always strings, so the
export helper must stringify the int keys and the import helper must restore
them exactly. These tests lock that contract.
"""

from __future__ import annotations

import json

from faultflow.scan.pattern_export import (
    scan_pattern_from_dict,
    scan_pattern_to_dict,
)
from faultflow.scan.protocol import ScanPattern


def _sample_pattern() -> ScanPattern:
    return ScanPattern(
        load_seqs={0: [True, False, True], 1: [False, False]},
        capture_pi_values={"pi_a": True, "pi_b": False},
        expected_unload={0: [False, True, True], 1: [True, False]},
    )


def test_to_dict_is_json_serializable_with_string_keys() -> None:
    d = scan_pattern_to_dict(_sample_pattern())
    back = json.loads(json.dumps(d))
    assert set(back["load_seqs"].keys()) == {"0", "1"}
    assert back["capture_pi_values"] == {"pi_a": True, "pi_b": False}


def test_roundtrip_is_exact() -> None:
    p = _sample_pattern()
    restored = scan_pattern_from_dict(json.loads(json.dumps(scan_pattern_to_dict(p))))
    assert restored.load_seqs == p.load_seqs
    assert restored.capture_pi_values == p.capture_pi_values
    assert restored.expected_unload == p.expected_unload


def test_roundtrip_restores_int_chain_keys() -> None:
    restored = scan_pattern_from_dict(scan_pattern_to_dict(_sample_pattern()))
    assert all(isinstance(k, int) for k in restored.load_seqs)
    assert all(isinstance(k, int) for k in restored.expected_unload)


def test_empty_pattern_roundtrips() -> None:
    empty = ScanPattern(load_seqs={}, capture_pi_values={}, expected_unload={})
    restored = scan_pattern_from_dict(scan_pattern_to_dict(empty))
    assert restored == empty


def test_bool_values_are_preserved() -> None:
    restored = scan_pattern_from_dict(scan_pattern_to_dict(_sample_pattern()))
    for seq in restored.load_seqs.values():
        assert all(isinstance(b, bool) for b in seq)
    assert all(isinstance(v, bool) for v in restored.capture_pi_values.values())


def test_stuck_at_pattern_exports_no_launch() -> None:
    d = scan_pattern_to_dict(_sample_pattern())
    assert "launch" not in d
    assert "launch_scan_in" not in d


def test_launch_on_shift_roundtrips_its_scan_in_bits() -> None:
    p = ScanPattern(
        load_seqs={0: [True], 1: [False]},
        capture_pi_values={},
        expected_unload={0: [False], 1: [True]},
        launch="los",
        launch_scan_in={0: True, 1: False},
    )
    d = json.loads(json.dumps(scan_pattern_to_dict(p)))
    assert d["launch"] == "los"
    assert d["launch_scan_in"] == {"0": True, "1": False}
    assert scan_pattern_from_dict(d) == p


def test_launch_on_capture_roundtrips() -> None:
    p = ScanPattern(
        load_seqs={0: [True]},
        capture_pi_values={},
        expected_unload={0: [False]},
        launch="loc",
    )
    restored = scan_pattern_from_dict(json.loads(json.dumps(scan_pattern_to_dict(p))))
    assert restored == p
    assert restored.launch_scan_in == {}
