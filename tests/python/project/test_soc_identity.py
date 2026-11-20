"""Which block faults a composed SoC's fault site stands for
(faultflow.project.identity), and which block input stems have no SoC site."""

from __future__ import annotations

import pytest

from faultflow.project.identity import (
    BlockSites,
    IdentityError,
    merged_inputs,
    soc_identities,
)


def _stem(net: int) -> dict[str, object]:
    return {"site_key": f"net:{net}:stem", "kind": "stem", "yosys_net_id": net}


def _branch(net: int, consumer: str, pin: str) -> dict[str, object]:
    return {
        "site_key": f"net:{net}:branch:{consumer}:{pin}",
        "kind": "branch",
        "yosys_net_id": net,
        "consumer_instance": consumer,
        "input_pin": pin,
    }


# u_a drives its output port bit, net 3 in it; u_b reads it on its input port bit,
# net 5 in it; the glue wires the two together, SoC net 9.
WIRED = {
    "u_a": BlockSites("blkA", (), frozenset(), {3: 9}),
    "u_b": BlockSites("blkB", (), frozenset({5}), {5: 9}),
}


def test_a_bare_wire_between_two_blocks_is_both_blocks_port_stems() -> None:
    identities = soc_identities([_stem(9)], WIRED)
    assert identities == {
        "net:9:stem": [("blkA", "net:3:stem"), ("blkB", "net:5:stem")]
    }
    assert merged_inputs(WIRED, identities, [_stem(9)]) == set()


def test_a_block_input_the_soc_fans_out_has_no_soc_site() -> None:
    """u_b reads its port bit in two cells, and the SoC net reaches a glue cell too:
    the readers' branches are the SoC's sites, each u_b's branch, and u_b's port
    stem, which covered both its readers at once, is none."""
    readers = (_stem(5), _branch(5, "c1", "A"), _branch(5, "c2", "A"))
    blocks = {**WIRED, "u_b": BlockSites("blkB", readers, frozenset({5}), {5: 9})}
    rows = [
        _stem(9),
        _branch(9, "u_b__c1", "A"),
        _branch(9, "u_b__c2", "A"),
        _branch(9, "glue_xor", "B"),
    ]
    identities = soc_identities(rows, blocks)
    assert identities["net:9:branch:u_b__c1:A"] == [("blkB", "net:5:branch:c1:A")]
    assert ("blkB", "net:5:stem") not in {i for ids in identities.values() for i in ids}
    assert merged_inputs(blocks, identities, rows) == {("blkB", "net:5:stem")}


def test_a_block_input_stem_lost_on_a_net_the_soc_does_not_fan_out_is_refused() -> None:
    """A wire with one reader is one SoC site standing for both port stems: an
    identity missing there is no merge but a lost fault."""
    identities = {"net:9:stem": [("blkA", "net:3:stem")]}
    with pytest.raises(IdentityError, match=r"blkB.*net:5:stem"):
        merged_inputs(WIRED, identities, [_stem(9)])
