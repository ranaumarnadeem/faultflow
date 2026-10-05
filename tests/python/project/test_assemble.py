"""Tests for `faultflow.project.assemble`: composing a flat, single-top Yosys-JSON
SoC netlist out of a glue synthesis (blocks read via `-lib` so they survive as
blackbox instance cells) plus each block's own frozen, already-synthesized /
scanned / wrapped generic JSON.

See `faultflow/project/assemble.py` module docstring for the full algorithm.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.assemble import (
    AssembleError,
    assemble_soc,
    block_stub_verilog,
    compose_soc,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.json"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _block_ports_fixture() -> dict[str, Any]:
    """Tiny block JSON with mixed 1-bit and multi-bit ports (for stub Verilog)."""
    return {
        "modules": {
            "block_x": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "a": {"direction": "input", "bits": [3, 4, 5, 6]},
                    "y": {"direction": "output", "bits": [7]},
                },
                "cells": {},
                "netnames": {},
            }
        }
    }


def _glue_json(inst_ports: dict[str, list[int]]) -> dict[str, Any]:
    """A tiny glue module `soc` with one real Sky130 cell and one instance cell
    `u_a` of type `block_a`, whose `connections` map is `inst_ports`."""
    return {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "top_in": {"direction": "input", "bits": [3]},
                    "top_out": {"direction": "output", "bits": [8]},
                },
                "cells": {
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        # Drives net 13, NOT top_out (8) -- top_out is driven
                        # by u_a's own "y" connection; having g0 ALSO drive it
                        # would be a pre-existing double-driver conflict with
                        # nothing to do with compose_soc's own correctness.
                        "connections": {"A": [3], "X": [13]},
                    },
                    # Drives net 9 -- the conventional "b" net most callers use
                    # -- from top_in, so a block's "b" port is electrically
                    # complete (has a real driver) rather than an intentional
                    # gap; tests that want a genuinely orphaned net use a
                    # different id (99) precisely to avoid this cell.
                    "g1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [3], "X": [9]},
                    },
                    "u_a": {
                        "hide_name": 0,
                        "type": "block_a",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            name: ("input" if name != "y" else "output")
                            for name in inst_ports
                        },
                        "connections": dict(inst_ports),
                    },
                },
                "netnames": {
                    "clk": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "top_in": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "top_out": {"hide_name": 0, "bits": [8], "attributes": {}},
                },
            },
            "block_a": {
                "attributes": {"blackbox": "1"},
                "ports": {
                    name: {
                        "direction": "input" if name != "y" else "output",
                        "bits": [0],
                    }
                    for name in inst_ports
                },
                "cells": {},
                "netnames": {},
            },
        },
    }


def _block_a_json(
    net_clk: int = 2, net_a: int = 3, net_b: int = 4, net_y: int = 5
) -> dict[str, Any]:
    """A tiny block_a generic JSON: two cells wired between clk/a/b -> y, using
    deliberately LOW net ids so they would collide with plausible glue ids if not
    remapped. One cell has a constant-tied input (A2=1'b1 style) to prove constants
    pass through unchanged."""
    internal = net_y + 1  # internal net between the two cells
    return {
        "creator": "test",
        "modules": {
            "block_a": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [net_clk]},
                    "a": {"direction": "input", "bits": [net_a]},
                    "b": {"direction": "input", "bits": [net_b]},
                    "y": {"direction": "output", "bits": [net_y]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        "connections": {"A": [net_a], "B": [net_b], "X": [internal]},
                    },
                    "c1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        # B tied to constant "1" -- must pass through unremapped.
                        "connections": {"A": [internal], "B": ["1"], "X": [net_y]},
                    },
                },
                "netnames": {
                    "clk": {"hide_name": 0, "bits": [net_clk], "attributes": {}},
                    "a": {"hide_name": 0, "bits": [net_a], "attributes": {}},
                    "b": {"hide_name": 0, "bits": [net_b], "attributes": {}},
                    "y": {"hide_name": 0, "bits": [net_y], "attributes": {}},
                    "internal_n": {
                        "hide_name": 0,
                        "bits": [internal],
                        "attributes": {},
                    },
                },
            }
        },
    }


def _all_int_bits(module: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for port in module.get("ports", {}).values():
        out.update(b for b in port.get("bits", []) if isinstance(b, int))
    for net in module.get("netnames", {}).values():
        out.update(b for b in net.get("bits", []) if isinstance(b, int))
    for cell in module.get("cells", {}).values():
        for bits in cell.get("connections", {}).values():
            out.update(b for b in bits if isinstance(b, int))
    return out


def _assert_single_driver(module: dict[str, Any]) -> None:
    """Every net READ by a cell (an input-direction pin, per that cell's own
    `port_directions`) or exposed as a top-level output must have EXACTLY one
    driver: some cell's output-direction pin, or -- for a net that is also a
    top-level input -- the outside world. A constant ("0"/"1"/"x"/"z"
    appearing directly in a `connections`/`bits` list) is a literal value, not
    a net id, and needs no driver.

    This is the regression guard for compose_soc's MERGE/CONST_IN/CONST_OUT
    bug class: a net silently left with zero drivers (or, for a different
    flavor of the same bug class, more than one) is exactly what a flat
    per-instance remap dict used to produce.

    Hand-built fixtures only: in a REAL Yosys-synthesized netlist, library
    cells carry no `port_directions`, so this would count no library cell as
    a driver. Use Yosys `check` (Liberty-aware) on real netlists instead."""
    ports = module.get("ports", {})
    top_level_inputs = {
        b
        for port in ports.values()
        if isinstance(port, dict) and port.get("direction") == "input"
        for b in port.get("bits", [])
        if isinstance(b, int)
    }
    top_level_outputs = {
        b
        for port in ports.values()
        if isinstance(port, dict) and port.get("direction") == "output"
        for b in port.get("bits", [])
        if isinstance(b, int)
    }

    driver_count: dict[int, int] = {}
    read_nets: set[int] = set(top_level_outputs)

    for cell in module.get("cells", {}).values():
        if not isinstance(cell, dict):
            continue
        directions = cell.get("port_directions", {})
        for pin, bits in cell.get("connections", {}).items():
            direction = directions.get(pin)
            for b in bits:
                if not isinstance(b, int):
                    continue
                if direction == "output":
                    driver_count[b] = driver_count.get(b, 0) + 1
                elif direction == "input":
                    read_nets.add(b)

    for net in sorted(read_nets):
        if net in top_level_inputs:
            continue  # driven externally, by definition
        count = driver_count.get(net, 0)
        assert count == 1, f"net {net} has {count} driver(s), expected exactly 1"


# --------------------------------------------------------------------------- #
# 1. block_stub_verilog                                                       #
# --------------------------------------------------------------------------- #


def test_block_stub_verilog_declares_correct_ports() -> None:
    block_json = _block_ports_fixture()
    stub = block_stub_verilog(block_json, "block_x")

    assert "(* blackbox *)" in stub
    assert "module block_x" in stub
    assert "input clk" in stub
    assert "input [3:0] a" in stub
    assert "output y" in stub


def test_block_stub_verilog_with_parameters() -> None:
    block_json = _block_ports_fixture()

    stub = block_stub_verilog(
        block_json, "block_x", parameters={"ADDR_WIDTH": 4, "LABEL": "foo"}
    )

    assert "(* blackbox *)" in stub
    assert (
        'module block_x #(parameter ADDR_WIDTH = 4, parameter LABEL = "foo")(' in stub
    )
    assert "input clk" in stub
    assert "input [3:0] a" in stub
    assert "output y" in stub


def test_block_stub_verilog_parameters_none_is_byte_identical_to_before() -> None:
    block_json = _block_ports_fixture()
    assert block_stub_verilog(block_json, "block_x") == block_stub_verilog(
        block_json, "block_x", parameters=None
    )
    assert block_stub_verilog(block_json, "block_x") == block_stub_verilog(
        block_json, "block_x", parameters={}
    )


# --------------------------------------------------------------------------- #
# 2. compose_soc splices block cells with remapped nets                       #
# --------------------------------------------------------------------------- #


def test_compose_soc_splices_block_cells_with_remapped_nets() -> None:
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    # Deliberately collide: block_a's internal net ids (2,3,4,5,6) overlap with
    # glue's own net ids (2=clk, 3=top_in/a, 8=top_out/y).
    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    glue_before_ids = _all_int_bits(glue["modules"]["soc"])

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )

    module = result["modules"]["soc"]
    cells = module["cells"]

    # (a) instance cell gone
    assert "u_a" not in cells

    # (b) block's cells present with u_a__ prefix
    assert "u_a__c0" in cells
    assert "u_a__c1" in cells

    # (c) + (d) net-id sanity: every net used by spliced cells is either one of
    # the glue-space port-boundary nets (2, 9, 8 for clk/b/y... note "a" -> glue
    # net 3 too) or a fresh id > any id used anywhere in the original glue.
    max_glue_id = max(glue_before_ids)
    spliced_nets: set[int] = set()
    for name in ("u_a__c0", "u_a__c1"):
        for bits in cells[name]["connections"].values():
            spliced_nets.update(b for b in bits if isinstance(b, int))

    port_boundary_nets = {2, 3, 9, 8}  # clk, a, b, y (glue-space)
    for net in spliced_nets:
        assert (
            net in port_boundary_nets or net > max_glue_id
        ), f"net {net} neither a port-boundary net nor freshly allocated"

    # No accidental collisions: glue's OWN cell (g0) nets vs spliced (non-port)
    # nets must not overlap.
    g0_nets = {
        b
        for bits in cells["g0"]["connections"].values()
        for b in bits
        if isinstance(b, int)
    }
    internal_spliced = spliced_nets - port_boundary_nets
    assert g0_nets.isdisjoint(internal_spliced)

    # (e) constant "1" on c1.B passed through unchanged.
    assert cells["u_a__c1"]["connections"]["B"] == ["1"]

    _assert_single_driver(module)


def test_compose_soc_tags_every_cell_and_returns_each_blocks_remap() -> None:
    """tag_blocks: every spliced cell names its block and its own name there (only
    then: compression and compaction compose without them). remaps: each block
    net's composed net -- a boundary bit its glue net, an internal bit a fresh one."""
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)
    remaps: dict[str, dict[int, int | str]] = {}
    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
        block_names={"u_a": "blkA"},
        tag_blocks=True,
        remaps=remaps,
    )
    cells = result["modules"]["soc"]["cells"]
    for name in ("c0", "c1"):
        attributes = cells[f"u_a__{name}"]["attributes"]
        assert (attributes["faultflow_block"], attributes["faultflow_cell"]) == (
            "blkA",
            name,
        )
    assert "faultflow_block" not in cells["g0"]["attributes"]
    remap = remaps["u_a"]
    assert (remap[2], remap[3], remap[4], remap[5]) == (2, 3, 9, 8)
    internal = cells["u_a__c0"]["connections"]["X"][0]
    assert remap[6] == internal and internal not in (2, 3, 8, 9, 13)
    untagged = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )
    assert (
        "faultflow_cell"
        not in untagged["modules"]["soc"]["cells"]["u_a__c0"]["attributes"]
    )


def test_compose_soc_tolerates_a_genuinely_unconnected_block_port_key_absent() -> None:
    """A block port the glue instantiation never wires at all (valid Verilog --
    e.g. an unused diagnostic output nobody connected) must not raise; its bits
    get a fresh internal net, same treatment as a constant-tied bit with no
    glue-space net to bind to.

    This covers the Yosys shape produced when the port is omitted entirely
    from the instantiation's port list -- both "connections" and
    "port_directions" drop the key. See the sibling
    `..._empty_connection_list` test below for the OTHER real Yosys shape
    (key present, value `[]`), produced when the instantiation instead uses
    explicit empty parens (`.port()`) -- confirmed by direct inspection of
    real Yosys JSON output for both forms; a prior fix here only handled the
    key-absent shape and still raised on the empty-list one."""
    # "b" is deliberately given a glue-space id (99) far outside every other id
    # used anywhere else in this fixture, so once its one and only reference
    # (u_a's own connection) is deleted below, it becomes fully orphaned --
    # readable as "not a live glue net anymore", not easily confused with a
    # fresh id that's merely unrelated to the ids still actually in use.
    glue = _glue_json({"clk": [2], "a": [3], "b": [99], "y": [8]})
    del glue["modules"]["soc"]["cells"]["u_a"]["connections"]["b"]
    del glue["modules"]["soc"]["cells"]["u_a"]["port_directions"]["b"]

    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )

    cells = result["modules"]["soc"]["cells"]
    assert "u_a__c0" in cells
    assert "u_a__c1" in cells
    # c0's B input (block_a's "b" port) got a fresh internal id, not an error,
    # and doesn't collide with any glue-space net still actually in use
    # (clk=2, a/top_in=3, y/top_out=8, g0's own nets).
    b_net = cells["u_a__c0"]["connections"]["B"][0]
    assert isinstance(b_net, int)
    live_glue_nets = {2, 3, 8} | {
        b for bits in cells["g0"]["connections"].values() for b in bits
    }
    assert b_net not in live_glue_nets


def test_compose_soc_tolerates_a_genuinely_unconnected_block_port_empty_connection_list() -> (  # noqa: E501
    None
):
    """Same tolerance as above, but for the OTHER real Yosys shape: the port
    key is present in both "connections" and "port_directions", just with an
    empty bit list (`[]`) -- what Yosys emits for an instantiation using
    explicit empty parens (`.port()`), the pattern a code-generated wrapper
    (e.g. autoMBIST's) tends to use for every declared port whether wired or
    not. `inst_connections.get(port_name)` returns `[]` here, not `None`, so
    an `is None` check alone does not catch it."""
    glue = _glue_json({"clk": [2], "a": [3], "b": [99], "y": [8]})
    glue["modules"]["soc"]["cells"]["u_a"]["connections"]["b"] = []

    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )

    cells = result["modules"]["soc"]["cells"]
    assert "u_a__c0" in cells
    assert "u_a__c1" in cells
    b_net = cells["u_a__c0"]["connections"]["B"][0]
    assert isinstance(b_net, int)
    live_glue_nets = {2, 3, 8} | {
        b for bits in cells["g0"]["connections"].values() for b in bits
    }
    assert b_net not in live_glue_nets


# --------------------------------------------------------------------------- #
# 4. compose_soc: two blocks, no net collision                                #
# --------------------------------------------------------------------------- #


def test_compose_soc_two_blocks_no_net_collision() -> None:
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    module = glue["modules"]["soc"]
    # Add a second instance cell u_b of the same block type, wired to fully
    # separate glue-space nets so the assertion is a clean empty intersection.
    module["ports"]["b_in"] = {"direction": "input", "bits": [10]}
    module["ports"]["b_in2"] = {"direction": "input", "bits": [11]}
    module["ports"]["b_out"] = {"direction": "output", "bits": [12]}
    module["netnames"]["b_in"] = {"hide_name": 0, "bits": [10], "attributes": {}}
    module["netnames"]["b_in2"] = {"hide_name": 0, "bits": [11], "attributes": {}}
    module["netnames"]["b_out"] = {"hide_name": 0, "bits": [12], "attributes": {}}
    module["cells"]["u_b"] = {
        "hide_name": 0,
        "type": "block_a",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            "clk": "input",
            "a": "input",
            "b": "input",
            "y": "output",
        },
        # Fully separate glue-space nets from u_a (which uses 2, 3, 9, 8).
        "connections": {"clk": [2], "a": [10], "b": [11], "y": [12]},
    }
    glue["modules"]["block_a"] = {
        "attributes": {"blackbox": "1"},
        "ports": {
            "clk": {"direction": "input", "bits": [0]},
            "a": {"direction": "input", "bits": [0]},
            "b": {"direction": "input", "bits": [0]},
            "y": {"direction": "output", "bits": [0]},
        },
        "cells": {},
        "netnames": {},
    }

    # Same block type/JSON for both instances -- internal net-id spaces
    # originally overlap (both start at 2,3,4,5).
    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a, "u_b": block_a},
        block_module={"u_a": "block_a", "u_b": "block_a"},
    )

    cells = result["modules"]["soc"]["cells"]
    assert "u_a" not in cells
    assert "u_b" not in cells

    a_nets: set[int] = set()
    b_nets: set[int] = set()
    for name, cell in cells.items():
        if name.startswith("u_a__"):
            a_nets.update(
                b
                for bits in cell["connections"].values()
                for b in bits
                if isinstance(b, int)
            )
        elif name.startswith("u_b__"):
            b_nets.update(
                b
                for bits in cell["connections"].values()
                for b in bits
                if isinstance(b, int)
            )

    assert a_nets, "expected u_a__ prefixed cells with net connections"
    assert b_nets, "expected u_b__ prefixed cells with net connections"
    assert a_nets.isdisjoint(b_nets)

    _assert_single_driver(result["modules"]["soc"])


# --------------------------------------------------------------------------- #
# 4b. compose_soc: MERGE resolution (union-find across boundary positions)    #
# --------------------------------------------------------------------------- #


def test_compose_soc_merge_input_output_passthrough() -> None:
    """A block's internal net backs BOTH an input port and an output port
    (e.g. a clock passed straight through to a second port name). The glue
    wires the two port names to DIFFERENT glue nets. The internal reader must
    see the input-side net (guaranteed externally driven), and the
    output-side glue net -- previously "driven" only by the now-deleted
    instance cell -- must be substituted to that same value everywhere the
    ORIGINAL glue referenced it, not left dangling."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [1]},
                    "p_a": {"direction": "input", "bits": [2]},
                    "p_out": {"direction": "output", "bits": [3]},
                },
                "cells": {
                    # Reads net 9 expecting it to be driven by u_m's "p_out"
                    # -- exactly the net a flat remap dict leaves dangling
                    # once u_m is deleted.
                    "g_reader": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [9], "X": [3]},
                    },
                    "u_m": {
                        "hide_name": 0,
                        "type": "block_m",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"p_in": "input", "p_out": "output"},
                        "connections": {"p_in": [2], "p_out": [9]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_m = {
        "creator": "test",
        "modules": {
            "block_m": {
                "attributes": {"top": "1"},
                # p_in and p_out are the SAME internal bit (5): p_out is
                # p_in wired straight through, no gate in between.
                "ports": {
                    "p_in": {"direction": "input", "bits": [5]},
                    "p_out": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [5], "Y": [6]},
                    },
                },
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_m": block_m},
        block_module={"u_m": "block_m"},
    )

    module = result["modules"]["soc"]
    cells = module["cells"]
    assert "u_m" not in cells

    # The internal reader (c0.A) gets the INPUT-side net (2) -- the one
    # guaranteed to stay externally driven -- not the output-side net (9).
    assert cells["u_m__c0"]["connections"]["A"] == [2]

    # The output-side glue net (9) is merged into 2 everywhere the ORIGINAL
    # glue referenced it: g_reader (untouched by any per-instance remap) must
    # now read 2, not the orphaned 9.
    assert cells["g_reader"]["connections"]["A"] == [2]

    _assert_single_driver(module)


def test_compose_soc_merge_output_output() -> None:
    """A block's internal net backs TWO output ports (e.g. two diagnostic
    status signals derived identically). The glue wires the two port names
    to DIFFERENT glue nets, each read by its own consumer. Both consumers
    must converge on the SAME (correctly driven) net."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [1]},
                    "p_in": {"direction": "input", "bits": [2]},
                },
                "cells": {
                    "g1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [20], "X": [22]},
                    },
                    "g2": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [21], "X": [23]},
                    },
                    "u_n": {
                        "hide_name": 0,
                        "type": "block_n",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "p_in": "input",
                            "q_out1": "output",
                            "q_out2": "output",
                        },
                        "connections": {
                            "p_in": [2],
                            "q_out1": [20],
                            "q_out2": [21],
                        },
                    },
                },
                "netnames": {},
            },
        },
    }
    block_n = {
        "creator": "test",
        "modules": {
            "block_n": {
                "attributes": {"top": "1"},
                # q_out1 and q_out2 are the SAME internal bit (7).
                "ports": {
                    "p_in": {"direction": "input", "bits": [5]},
                    "q_out1": {"direction": "output", "bits": [7]},
                    "q_out2": {"direction": "output", "bits": [7]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [5], "Y": [7]},
                    },
                },
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_n": block_n},
        block_module={"u_n": "block_n"},
    )
    module = result["modules"]["soc"]
    cells = module["cells"]

    driven = cells["u_n__c0"]["connections"]["Y"][0]
    assert isinstance(driven, int)
    assert cells["g1"]["connections"]["A"] == [driven]
    assert cells["g2"]["connections"]["A"] == [driven]

    _assert_single_driver(module)


def test_compose_soc_merge_within_one_port_shared_across_bit_positions() -> None:
    """A single internal net backs ALL FOUR bit positions of one multi-bit
    block OUTPUT port (e.g. a diagnostic module that broadcasts one bit
    across a 4-wide bus). The glue wires the 4 bit positions to 4 DIFFERENT
    glue nets, one per external consumer. All 4 consumers must converge on
    the SAME (correctly driven) net -- a flat per-instance remap dict keeps
    only the LAST bit position's target, leaving the other 3 consumers
    reading an undriven net."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [1]},
                    "p_in": {"direction": "input", "bits": [2]},
                },
                "cells": {
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [40], "X": [60]},
                    },
                    "g1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [41], "X": [61]},
                    },
                    "g2": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [42], "X": [62]},
                    },
                    "g3": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [43], "X": [63]},
                    },
                    "u_w": {
                        "hide_name": 0,
                        "type": "block_w",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"p_in": "input", "d_out": "output"},
                        "connections": {"p_in": [2], "d_out": [40, 41, 42, 43]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_w = {
        "creator": "test",
        "modules": {
            "block_w": {
                "attributes": {"top": "1"},
                "ports": {
                    "p_in": {"direction": "input", "bits": [5]},
                    # All 4 bit positions are the SAME internal net (30).
                    "d_out": {"direction": "output", "bits": [30, 30, 30, 30]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [5], "Y": [30]},
                    },
                },
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_w": block_w},
        block_module={"u_w": "block_w"},
    )
    module = result["modules"]["soc"]
    cells = module["cells"]

    driven = cells["u_w__c0"]["connections"]["Y"][0]
    assert isinstance(driven, int)
    for i in range(4):
        assert cells[f"g{i}"]["connections"]["A"] == [
            driven
        ], f"g{i} did not converge on the correctly-driven net"

    _assert_single_driver(module)


# --------------------------------------------------------------------------- #
# 4c. compose_soc: CONST_IN / CONST_OUT resolution                            #
# --------------------------------------------------------------------------- #


def test_compose_soc_const_in_glue_ties_block_input_to_constant() -> None:
    """The glue ties a block's input port to a constant (not a real net).
    The block's internal reader must see the CONSTANT directly, not a fresh
    undriven net -- what checking `isinstance(glue_bit, int)` alone used to
    produce, since a non-int glue value was silently skipped entirely."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {"clk": {"direction": "input", "bits": [1]}},
                "cells": {
                    "u_ci": {
                        "hide_name": 0,
                        "type": "block_ci",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"en": "input", "y": "output"},
                        "connections": {"en": ["0"], "y": [9]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_ci = {
        "creator": "test",
        "modules": {
            "block_ci": {
                "attributes": {"top": "1"},
                "ports": {
                    "en": {"direction": "input", "bits": [5]},
                    "y": {"direction": "output", "bits": [6]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [5], "X": [6]},
                    },
                },
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_ci": block_ci},
        block_module={"u_ci": "block_ci"},
    )
    module = result["modules"]["soc"]

    # c0's A input reads the LITERAL constant, not a fresh undriven net.
    assert module["cells"]["u_ci__c0"]["connections"]["A"] == ["0"]

    _assert_single_driver(module)


def test_compose_soc_const_out_block_output_is_itself_a_constant() -> None:
    """A block's own output port bit is ITSELF a constant literal (Yosys
    emits this for a provably-constant output), not a real internal net.
    Both a glue-native reader cell AND a top-level output port that
    referenced the (now-deleted) instance's output must show the constant,
    not an orphaned net id -- what skipping via `isinstance(bit, int)` alone
    used to leave undriven."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [1]},
                    "top_status": {"direction": "output", "bits": [70]},
                },
                "cells": {
                    "g_reader": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [70], "X": [71]},
                    },
                    "u_co": {
                        "hide_name": 0,
                        "type": "block_co",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"status": "output"},
                        "connections": {"status": [70]},
                    },
                },
                "netnames": {
                    "top_status": {
                        "hide_name": 0,
                        "bits": [70],
                        "attributes": {},
                    },
                },
            },
        },
    }
    block_co = {
        "creator": "test",
        "modules": {
            "block_co": {
                "attributes": {"top": "1"},
                # "status" is a provably-constant output: its own bit is the
                # literal "1", not a real net id.
                "ports": {"status": {"direction": "output", "bits": ["1"]}},
                "cells": {},
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_co": block_co},
        block_module={"u_co": "block_co"},
    )
    module = result["modules"]["soc"]

    # The glue-native reader now reads the constant directly.
    assert module["cells"]["g_reader"]["connections"]["A"] == ["1"]
    # The top-level output PORT bit is substituted to the constant too.
    assert module["ports"]["top_status"]["bits"] == ["1"]

    _assert_single_driver(module)


# --------------------------------------------------------------------------- #
# 4d. compose_soc: contradiction errors                                       #
# --------------------------------------------------------------------------- #


def test_compose_soc_conflicting_0_and_1_constants_raises() -> None:
    """A block's internal net is tied to "0" via one port and "1" via
    another (a genuine contradiction) -- must raise, not silently pick one."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {"clk": {"direction": "input", "bits": [1]}},
                "cells": {
                    "u_bad": {
                        "hide_name": 0,
                        "type": "block_bad0",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"a_in": "input", "b_in": "input"},
                        "connections": {"a_in": ["0"], "b_in": ["1"]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_bad0 = {
        "creator": "test",
        "modules": {
            "block_bad0": {
                "attributes": {"top": "1"},
                # a_in and b_in are the SAME internal bit.
                "ports": {
                    "a_in": {"direction": "input", "bits": [80]},
                    "b_in": {"direction": "input", "bits": [80]},
                },
                "cells": {},
                "netnames": {},
            }
        },
    }

    with pytest.raises(AssembleError, match="conflicting constant"):
        compose_soc(
            glue_json=glue,
            soc_top="soc",
            blocks={"u_bad": block_bad0},
            block_module={"u_bad": "block_bad0"},
        )


def test_compose_soc_two_top_level_inputs_shorted_together_raises() -> None:
    """A block's internal net is tied to TWO DIFFERENT top-level primary
    inputs (a genuine short between two independently-driven pins) -- must
    raise, not silently pick one and drop the other's connection."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [1]},
                    "in_x": {"direction": "input", "bits": [90]},
                    "in_y": {"direction": "input", "bits": [91]},
                },
                "cells": {
                    "u_bad": {
                        "hide_name": 0,
                        "type": "block_bad1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"p1": "input", "p2": "input"},
                        "connections": {"p1": [90], "p2": [91]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_bad1 = {
        "creator": "test",
        "modules": {
            "block_bad1": {
                "attributes": {"top": "1"},
                # p1 and p2 are the SAME internal bit.
                "ports": {
                    "p1": {"direction": "input", "bits": [95]},
                    "p2": {"direction": "input", "bits": [95]},
                },
                "cells": {},
                "netnames": {},
            }
        },
    }

    with pytest.raises(AssembleError, match="shorted together"):
        compose_soc(
            glue_json=glue,
            soc_top="soc",
            blocks={"u_bad": block_bad1},
            block_module={"u_bad": "block_bad1"},
        )


def test_compose_soc_width_mismatch_glue_wider_than_block_raises() -> None:
    """The old check only raised when the glue side was NARROWER than the
    block's port; a glue connection WIDER than the block's own port must
    raise too -- Yosys always emits connections at the stub's own width, so
    an exact-match check is safe."""
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {"clk": {"direction": "input", "bits": [1]}},
                "cells": {
                    "u_bad": {
                        "hide_name": 0,
                        "type": "block_bad2",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"p": "input"},
                        # 3 bits connected, but the block's own "p" is 2 wide.
                        "connections": {"p": [200, 201, 202]},
                    },
                },
                "netnames": {},
            },
        },
    }
    block_bad2 = {
        "creator": "test",
        "modules": {
            "block_bad2": {
                "attributes": {"top": "1"},
                "ports": {"p": {"direction": "input", "bits": [100, 101]}},
                "cells": {},
                "netnames": {},
            }
        },
    }

    with pytest.raises(AssembleError, match="width mismatch"):
        compose_soc(
            glue_json=glue,
            soc_top="soc",
            blocks={"u_bad": block_bad2},
            block_module={"u_bad": "block_bad2"},
        )


def _out_block(module: str, bits: list[Any], *, driven: bool = False) -> dict[str, Any]:
    """A one-output block. `bits` is the output port's own bit list: an int
    for a real net, or a literal -- Yosys writes an undriven output as "x" and
    a provably constant one as "0"/"1". `driven=True` adds an inverter that
    drives the (int) output from an input `a`."""
    ports: dict[str, Any] = {"o": {"direction": "output", "bits": bits}}
    cells: dict[str, Any] = {}
    if driven:
        ports["a"] = {"direction": "input", "bits": [2]}
        cells["inv"] = {
            "hide_name": 0,
            "type": "sky130_fd_sc_hd__inv_1",
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [2], "Y": bits},
        }
    return {"modules": {module: {"ports": ports, "cells": cells, "netnames": {}}}}


def _compose_outputs(
    insts: dict[str, tuple[dict[str, Any], str, int]], readers: list[int]
) -> dict[str, Any]:
    """Glue where each instance's output `o` drives glue net `net`, and a BUF
    `g<net>` reads each net in `readers` onto its own top-level output. A
    driven block's input `a` comes from top-level input net 1."""
    cells: dict[str, Any] = {}
    for name, (block, module, net) in insts.items():
        conns: dict[str, Any] = {"o": [net]}
        dirs = {"o": "output"}
        if "a" in block["modules"][module]["ports"]:
            conns["a"] = [1]
            dirs["a"] = "input"
        cells[name] = {
            "hide_name": 0,
            "type": module,
            "parameters": {},
            "attributes": {},
            "port_directions": dirs,
            "connections": conns,
        }
    for net in readers:
        cells[f"g{net}"] = {
            "hide_name": 0,
            "type": "sky130_fd_sc_hd__buf_1",
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "X": "output"},
            "connections": {"A": [net], "X": [100 + net]},
        }
    ports: dict[str, Any] = {"a_in": {"direction": "input", "bits": [1]}}
    ports.update(
        {f"q{net}": {"direction": "output", "bits": [100 + net]} for net in readers}
    )
    glue = {
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {},
            }
        }
    }
    return compose_soc(
        glue,
        "soc",
        {name: block for name, (block, _, _) in insts.items()},
        {name: module for name, (_, module, _) in insts.items()},
    )["modules"]["soc"]


def test_compose_soc_constants_never_link_unrelated_nets() -> None:
    """Two separate nets, one {undriven output, constant-1 output} and one
    {undriven output, constant-0 output}, are each consistent on their own. A
    shared "x" (or constant) union-find node used to merge them into one
    class and raise a false "conflicting constant drivers" error."""
    und, one, zero = (
        _out_block("und", ["x"]),
        _out_block("one", ["1"]),
        _out_block("zero", ["0"]),
    )
    module = _compose_outputs(
        {
            "u1": (und, "und", 9),
            "u2": (one, "one", 9),
            "u3": (und, "und", 12),
            "u4": (zero, "zero", 12),
        },
        readers=[9, 12],
    )
    assert module["cells"]["g9"]["connections"]["A"] == ["1"]
    assert module["cells"]["g12"]["connections"]["A"] == ["0"]


def test_compose_soc_undriven_output_is_not_forced_to_an_unrelated_constant() -> None:
    """An undriven block output alone on net 20 must not pick up the constant
    that a DIFFERENT net (9) carries -- it used to, through the shared "x"
    node. Net 20 has no driver in the design, so it stays a plain net."""
    und, one = _out_block("und", ["x"]), _out_block("one", ["1"])
    module = _compose_outputs(
        {"u1": (und, "und", 9), "u2": (one, "one", 9), "u5": (und, "und", 20)},
        readers=[9, 20],
    )
    assert module["cells"]["g9"]["connections"]["A"] == ["1"]
    assert module["cells"]["g20"]["connections"]["A"] == [20]


def test_compose_soc_undriven_output_does_not_override_a_real_driver() -> None:
    """An undriven output ("x") sharing a glue net with a cell-driven output
    contributes nothing: the driving inverter's OUTPUT pin must keep the net,
    not be rewritten to "x" (which the C++ core maps onto the global CONST0
    net, corrupting every 1'b0 tie in the design)."""
    und, drv = _out_block("und", ["x"]), _out_block("drv", [3], driven=True)
    module = _compose_outputs(
        {"u1": (und, "und", 9), "u2": (drv, "drv", 9)}, readers=[9]
    )
    assert module["cells"]["u2__inv"]["connections"]["Y"] == [9]
    assert module["cells"]["g9"]["connections"]["A"] == [9]
    _assert_single_driver(module)


def test_compose_soc_constant_output_shorted_to_a_driven_output_raises() -> None:
    """A constant block output and a cell-driven block output on the same net
    are two drivers. Substituting the constant would rewrite the driving
    cell's OUTPUT pin; raise instead (the C++ core raises "Multiple drivers"
    for the equivalent two-cell case)."""
    one, drv = _out_block("one", ["1"]), _out_block("drv", [3], driven=True)
    with pytest.raises(AssembleError, match="multiple drivers"):
        _compose_outputs({"u1": (one, "one", 9), "u2": (drv, "drv", 9)}, readers=[9])


@pytest.mark.parametrize("inst", ["C", "G", "B"])
@pytest.mark.parametrize("top_level_input", [True, False])
def test_compose_soc_instance_name_matching_a_key_tag_is_harmless(
    inst: str, top_level_input: bool
) -> None:
    """Union-find keys are tagged tuples; an instance literally named like a
    tag ("C", "G", "B") must not be misread as a constant/glue key. With an
    instance-first block key this wrote the string 'B' into the netlist as a
    net (inst "C"), raised a bogus "tied to constant" error (inst "C" on a
    top-level input), or crashed comparing str with int (inst "G")."""
    net = 2 if top_level_input else 7
    ports = {"clk": {"direction": "input", "bits": [1]}}
    cells: dict[str, Any] = {}
    if top_level_input:
        ports["p_in"] = {"direction": "input", "bits": [2]}
    else:
        cells["drv"] = {
            "hide_name": 0,
            "type": "sky130_fd_sc_hd__buf_1",
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "X": "output"},
            "connections": {"A": [1], "X": [7]},
        }
    cells[inst] = {
        "hide_name": 0,
        "type": "blk",
        "parameters": {},
        "attributes": {},
        "port_directions": {"p": "input"},
        "connections": {"p": [net]},
    }
    glue = {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {},
            }
        },
    }
    block = {
        "creator": "test",
        "modules": {
            "blk": {
                "attributes": {"top": "1"},
                "ports": {"p": {"direction": "input", "bits": [5]}},
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [5], "Y": [6]},
                    }
                },
                "netnames": {},
            }
        },
    }

    result = compose_soc(
        glue_json=glue, soc_top="soc", blocks={inst: block}, block_module={inst: "blk"}
    )
    module = result["modules"]["soc"]
    assert module["cells"][f"{inst}__c0"]["connections"]["A"] == [net]
    _assert_single_driver(module)


# --------------------------------------------------------------------------- #
# 5. assemble_soc end-to-end                                                  #
# --------------------------------------------------------------------------- #


def test_assemble_soc_end_to_end(tmp_path: Path, require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    from faultflow.runner.runner import _load_core
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge
    from faultflow.wrap.ports import wrap_ports

    core = _load_core()
    assert core is not None

    def _build_wrapped_block(name: str, op: str) -> Path:
        rtl = tmp_path / f"{name}.v"
        rtl.write_text(
            f"module {name}(input clk, input a, input b, output y);\n"
            f"  reg r;\n"
            f"  always @(posedge clk) r <= a {op} b;\n"
            f"  assign y = ~r;\n"
            f"endmodule\n",
            encoding="utf-8",
        )
        session = ProjectSession(output_root=tmp_path / "out" / name)
        bridge = TclBridge(session)
        bridge.call("read_netlist", str(rtl), "-top", name)
        bridge.call("use_lib_cells", "sky130")
        bridge.call("add_clock", "clk")
        bridge.call("synth")
        bridge.call("add_scan", "-chains", "1")

        cfg = session.materialize_config()
        scan_json = cfg.intermediate_dir / f"{name}_scan.json"
        assert scan_json.exists(), f"scan-stitched JSON not found at {scan_json}"
        scanned = json.loads(scan_json.read_text(encoding="utf-8"))
        wrapped = wrap_ports(scanned, name, wbr_model="scan")

        out_path = tmp_path / f"{name}_wrapped.json"
        out_path.write_text(json.dumps(wrapped), encoding="utf-8")
        return out_path

    block_a_path = _build_wrapped_block("block_a", "&")
    block_b_path = _build_wrapped_block("block_b", "|")

    soc_rtl = tmp_path / "soc.v"
    soc_rtl.write_text(
        "module soc(input clk, input a, input b, output y);\n"
        "  wire w;\n"
        "  wire a_scan_out, a_wbr_so, b_scan_out, b_wbr_so;\n"
        "  block_a u_a(\n"
        "    .clk(clk), .a(a), .b(b), .y(w),\n"
        "    .scan_en(1'b0), .scan_in(1'b0), .scan_out(a_scan_out),\n"
        "    .CLK(clk), .wbr_se(1'b0), .wbr_si(1'b0), .wbr_so(a_wbr_so)\n"
        "  );\n"
        "  block_b u_b(\n"
        "    .clk(clk), .a(w), .b(b), .y(y),\n"
        "    .scan_en(1'b0), .scan_in(1'b0), .scan_out(b_scan_out),\n"
        "    .CLK(clk), .wbr_se(1'b0), .wbr_si(1'b0), .wbr_so(b_wbr_so)\n"
        "  );\n"
        "endmodule\n",
        encoding="utf-8",
    )

    liberty = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.lib"
    if not liberty.exists():
        candidates = list((ROOT / "cells" / "sky130").glob("*.lib"))
        assert candidates, "no sky130 liberty file found for assemble_soc"
        liberty = candidates[0]

    output_json = tmp_path / "soc_composed.json"
    workdir = tmp_path / "assemble_work"

    result_path = assemble_soc(
        soc_rtl=soc_rtl,
        soc_top="soc",
        liberty=liberty,
        blocks={"u_a": block_a_path, "u_b": block_b_path},
        block_module={"u_a": "block_a", "u_b": "block_b"},
        output_json=output_json,
        workdir=workdir,
    )
    assert result_path == output_json
    assert output_json.exists()

    composed = json.loads(output_json.read_text(encoding="utf-8"))
    assert list(composed["modules"].keys()) == ["soc"]

    vectors = [
        {"clk": False, "a": False, "b": False},
        {"clk": False, "a": True, "b": True},
        {"clk": False, "a": True, "b": False},
    ]
    outputs = core.fault_free_outputs(
        str(output_json),
        str(CELL_MAP),
        vectors,
        ["clk", "a", "b"],
        ["y"],
    )
    assert len(outputs) == len(vectors)
