from __future__ import annotations

from faultflow.scan.protocol import serialize_vector
from faultflow.scan.protocol_constants import load_sequence, unload_sequence

MULTICHAIN_MANIFEST = {
    "chains": [
        {"index": 0, "length": 2},
        {"index": 1, "length": 2},
    ]
}
PSEUDO_MAP = {
    "ff0": {
        "ppi_port": "__ppi_ff0",
        "ppo_port": "__ppo_ff0",
        "chain_id": 0,
        "position_in_chain": 0,
    },
    "ff1": {
        "ppi_port": "__ppi_ff1",
        "ppo_port": "__ppo_ff1",
        "chain_id": 0,
        "position_in_chain": 1,
    },
    "ff2": {
        "ppi_port": "__ppi_ff2",
        "ppo_port": "__ppo_ff2",
        "chain_id": 1,
        "position_in_chain": 0,
    },
    "ff3": {
        "ppi_port": "__ppi_ff3",
        "ppo_port": "__ppo_ff3",
        "chain_id": 1,
        "position_in_chain": 1,
    },
}


def test_serialize_vector_asymmetric_chains() -> None:
    vector = {
        "__ppi_ff0": True,
        "__ppi_ff1": False,
        "__ppi_ff2": False,
        "__ppi_ff3": True,
        "__ppo_ff0": True,
        "__ppo_ff1": False,
        "__ppo_ff2": True,
        "__ppo_ff3": False,
        "D0": True,
    }
    pattern = serialize_vector(vector, PSEUDO_MAP, MULTICHAIN_MANIFEST)
    assert pattern.load_seqs[0] == [True, False]
    assert pattern.load_seqs[1] == [False, True]
    assert pattern.expected_unload[0] == [False, True]
    assert pattern.expected_unload[1] == [False, True]
    assert pattern.capture_pi_values == {"D0": True}


def test_real_pi_values_pass_through() -> None:
    vector = {"__ppi_ff0": False, "__ppo_ff0": True, "A": False, "B": True}
    pattern = serialize_vector(
        vector,
        {"ff0": PSEUDO_MAP["ff0"]},
        {"chains": [{"index": 0, "length": 1}]},
    )
    assert pattern.capture_pi_values == {"A": False, "B": True}


def test_single_chain_load_unload_reverse() -> None:
    targets = {0: True, 1: False, 2: True}
    assert load_sequence(targets, 3) == [True, False, True]
    assert unload_sequence(targets, 3) == list(reversed(load_sequence(targets, 3)))
