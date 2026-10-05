"""Which faults a fault of a composed SoC is: the block faults it stands for, or a
fault of the SoC's own glue.

Composing the SoC (faultflow.project.assemble) renames each block's cells
``<instance>__<cell>`` and remaps its nets, so a fault site of the SoC is a fault site
of a block when it sits on that block's cells or nets:
- A branch into a block's cell is that cell's input. In the block it is the branch
  into the same cell, or, when the cell is the only reader of one of the block's
  input ports, that port bit's stem. When the block drives the net itself and the
  SoC fans it out further, the block has no such fault -- its stem covered every
  reader at once -- and the branch is the SoC's own.
- A stem is its driver's output: the stem of the net in the block that drives it,
  or the glue's when nothing in a block does. When the SoC net has a single reader,
  and so no branches, the stem is that reader's input too: a block input port
  bit's stem -- and when the glue or a SoC input drives it, that alone.
So a wire from one block's output port to another's input port is ONE fault site of
the SoC with TWO identities: each block's port stem.

A block input port bit whose SoC net reaches more than the block has no fault of
its own in the SoC: its stem covered every reader in the block at once, which the
SoC splits into branches. Such a stem is the block boundary's, not the chip's
(:func:`merged_inputs`).

A site with no block identity is the glue's: ``("soc", key)``. A branch into an
EXTEST graybox's core blackbox (faultflow.wrap.graybox) stands for reads by several
core cells, none of which it is: it has no identity at all, and the aggregation
never needs one, since INTEST owns those reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SOC = "soc"

Identity = tuple[str, str]  # (scope: a block's name, or SOC; its site key)


@dataclass(frozen=True)
class BlockSites:
    """A block instance's name, its own netlist's fault site rows (the C++ core's
    list_site_keys), the nets of its input ports, and compose_soc's remap of its
    nets to the SoC's."""

    name: str
    rows: tuple[Mapping[str, Any], ...]
    inputs: frozenset[int]
    remap: Mapping[int, int | str]


def stem_key(net: int) -> str:
    return f"net:{net}:stem"


def branch_key(net: int, consumer: str, pin: str) -> str:
    return f"net:{net}:branch:{consumer}:{pin}"


def soc_identities(
    soc_rows: Iterable[Mapping[str, Any]],
    blocks: Mapping[str, BlockSites],
    *,
    stubs: Iterable[str] = (),
) -> dict[str, list[Identity]]:
    """SoC fault site key -> its identities. `soc_rows`: the C++ core's
    list_site_keys of the SoC netlist the faults were graded on; `blocks`: by
    instance; `stubs`: the cells standing for cores, whose branches have no
    identity."""
    rows = [dict(row) for row in soc_rows]
    stub_names = set(stubs)
    inverse: dict[str, dict[int, list[int]]] = {}
    branches: dict[str, set[str]] = {}
    for instance, block in blocks.items():
        nets: dict[int, list[int]] = {}
        for block_net, soc_net in block.remap.items():
            if isinstance(soc_net, int):
                nets.setdefault(soc_net, []).append(block_net)
        inverse[instance] = nets
        branches[instance] = {
            str(row["site_key"]) for row in block.rows if str(row["kind"]) == "branch"
        }
    fanned_out = {int(row["yosys_net_id"]) for row in rows if row["kind"] == "branch"}

    def owner_of(cell: str) -> tuple[str, str] | None:
        """(instance, its name in the block) for a block's cell."""
        for instance in blocks:
            prefix = f"{instance}__"
            if cell.startswith(prefix):
                return instance, cell[len(prefix) :]
        return None

    result: dict[str, list[Identity]] = {}
    for row in rows:
        key = str(row["site_key"])
        net = int(row["yosys_net_id"])
        if str(row["kind"]) == "branch":
            consumer = str(row["consumer_instance"])
            owner = None if consumer in stub_names else owner_of(consumer)
            if consumer in stub_names:
                result[key] = []
            elif owner is None:
                result[key] = [(SOC, key)]
            else:
                instance, cell = owner
                pin = str(row["input_pin"])
                block = blocks[instance]
                found: list[Identity] = []
                for b in inverse[instance].get(net, []):
                    own = branch_key(b, cell, pin)
                    if own in branches[instance]:
                        found.append((block.name, own))
                    elif b in block.inputs:
                        # The block's input pin: one reader inside it.
                        found.append((block.name, stem_key(b)))
                # Else the block drives the net and the SoC fans it out: a
                # fault of one reader the block's own stem covered with the
                # rest, the SoC's own.
                result[key] = found or [(SOC, key)]
            continue
        drivers: list[Identity] = []
        readers: list[Identity] = []
        for instance, block in blocks.items():
            for b in inverse[instance].get(net, []):
                (readers if b in block.inputs else drivers).append(
                    (block.name, stem_key(b))
                )
        if net in fanned_out:
            readers = []
        # Driven from outside every block (a glue cell, a SoC input), a net with
        # one reader in a block is that block's input pin alone.
        result[key] = (drivers + readers) or [(SOC, key)]
    return result


def merged_inputs(
    blocks: Mapping[str, BlockSites], identities: Mapping[str, list[Identity]]
) -> set[Identity]:
    """The stems of the blocks' input port bits no SoC fault site is: their SoC
    nets reach more than the block, so the SoC has their readers' branches
    instead. A flat run of the chip has no such fault."""
    found = {identity for ids in identities.values() for identity in ids}
    return {
        (block.name, stem_key(b))
        for block in blocks.values()
        for b in block.inputs
        if isinstance(block.remap.get(b), int)
        and (block.name, stem_key(b)) not in found
    }
