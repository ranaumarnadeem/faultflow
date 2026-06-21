from __future__ import annotations

import pytest

from faultflow.scan.protocol_constants import (
    LOAD_BIT_ORDER,
    UNLOAD_BIT_ORDER,
    load_sequence,
    unload_sequence,
)

# Expectations from tests/cpp/scan/test_scan_chain.cpp
THREE_FF_TARGET = {0: True, 1: False, 2: False}
CHAIN0_TWO_FF_TARGET = {0: True, 1: False}
CHAIN1_TWO_FF_TARGET = {0: False, 1: True}


def test_load_order_matches_three_ff_chain_fixture() -> None:
    assert load_sequence(THREE_FF_TARGET, 3) == [False, False, True]


def test_unload_order_matches_three_ff_chain_fixture() -> None:
    assert unload_sequence(THREE_FF_TARGET, 3) == [False, False, True]


def test_load_and_unload_use_scan_out_to_scan_in_position_order() -> None:
    targets = [
        THREE_FF_TARGET,
        CHAIN0_TWO_FF_TARGET,
        CHAIN1_TWO_FF_TARGET,
        {0: True, 1: True, 2: False, 3: True},
    ]
    for target in targets:
        length = max(target) + 1
        load = load_sequence(target, length)
        unload = unload_sequence(target, length)
        assert unload == load


def test_multi_chain_load_sequences_match_multichain_fixture() -> None:
    assert load_sequence(CHAIN0_TWO_FF_TARGET, 2) == [False, True]
    assert load_sequence(CHAIN1_TWO_FF_TARGET, 2) == [True, False]


def test_multi_chain_unload_sequences_match_multichain_fixture() -> None:
    assert unload_sequence(CHAIN0_TWO_FF_TARGET, 2) == [False, True]
    assert unload_sequence(CHAIN1_TWO_FF_TARGET, 2) == [True, False]


def test_asymmetric_chain_lengths_pad_with_false() -> None:
    target = {0: True, 2: True}
    assert load_sequence(target, 4) == [False, True, False, True]
    assert unload_sequence(target, 4) == [False, True, False, True]


def test_empty_chain_returns_empty_sequences() -> None:
    assert load_sequence({}, 0) == []
    assert unload_sequence({}, 0) == []


def test_negative_chain_length_raises() -> None:
    with pytest.raises(ValueError, match="chain_length"):
        load_sequence({}, -1)
    with pytest.raises(ValueError, match="chain_length"):
        unload_sequence({}, -1)


def test_ordering_constants_document_fixture_validated_convention() -> None:
    assert LOAD_BIT_ORDER == "descending_position"
    assert UNLOAD_BIT_ORDER == "descending_position"
