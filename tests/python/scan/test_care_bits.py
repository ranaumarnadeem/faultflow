from __future__ import annotations

import pytest

from faultflow.scan.care_bits import extract_scan_care_bits


def _oracle_requiring(required: dict[tuple[int, int], bool], target_id: int):
    """A fake detect() oracle: the fault is detected iff every required
    (chain, cycle) position in ``trial`` matches its required value.
    Positions not in ``required`` never affect detection -- pure don't-cares
    by construction, so extraction should drop exactly those."""

    def detect(trial: dict[int, list[bool]], targets: set[int]) -> set[int]:
        for (chain, cycle), value in required.items():
            if trial[chain][cycle] != value:
                return set()
        return {target_id} & targets

    return detect


@pytest.mark.unit
def test_extract_scan_care_bits_keeps_only_required_positions() -> None:
    load_seqs = {0: [False, True, False], 1: [False, False, True]}
    required = {(0, 1): True, (1, 2): True}
    detect = _oracle_requiring(required, target_id=42)

    care = extract_scan_care_bits(detect, load_seqs, max_chain_length=3, targets={42})

    assert set(care) == {(0, 1, True), (1, 2, True)}


@pytest.mark.unit
def test_extract_scan_care_bits_all_positions_are_dont_care_for_empty_targets() -> (
    None
):
    load_seqs = {0: [True, False], 1: [True, True]}
    detect = _oracle_requiring({(0, 0): True}, target_id=1)

    # targets=set() -- issubset(anything) is trivially True, so every
    # position looks like a don't-care regardless of what the oracle checks.
    care = extract_scan_care_bits(detect, load_seqs, max_chain_length=2, targets=set())

    assert care == []


@pytest.mark.unit
def test_extract_scan_care_bits_every_position_required_returns_full_cube() -> None:
    load_seqs = {0: [True, False, True]}
    required = {(0, 0): True, (0, 1): False, (0, 2): True}
    detect = _oracle_requiring(required, target_id=7)

    care = extract_scan_care_bits(detect, load_seqs, max_chain_length=3, targets={7})

    assert set(care) == {(0, 0, True), (0, 1, False), (0, 2, True)}


@pytest.mark.unit
def test_extract_scan_care_bits_does_not_mutate_load_seqs() -> None:
    load_seqs = {0: [True, False]}
    original = {0: [True, False]}
    detect = _oracle_requiring({(0, 0): True}, target_id=1)

    extract_scan_care_bits(detect, load_seqs, max_chain_length=2, targets={1})

    assert load_seqs == original
