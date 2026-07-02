"""aggregate.py must resolve WBC boundary faults on a FUSED scan-model EXTEST view.

`_sim_extest` runs ATPG on `fuse_wbr_into_view`'s output, not the pre-fusion graybox
-- and fusion allocates BRAND NEW net ids for the wrapper observe/control ports,
disjoint from the graybox's own numbering (confirmed empirically: a real composed
graybox's `__wi_add_sub.FROM_SYS` net 4 became a fresh net 127 in the fused view).
So the plain `_wbc_pin_index(scope.netlist, ...)` -- which reads the PRE-fusion
graybox -- can never match a `scan_extest` scope's actual fault net ids, and every
WBC-boundary fault silently misclassifies as "local", failing Guard 3's handoff
check with every single boundary fault reported unowned.

The fix re-derives the SAME (pure, deterministic) fusion from the graybox and
bridges through `fuse_wbr_into_view`'s returned `port_map` (keyed by the ORIGINAL
cell name, carrying the ORIGINAL sys-side net that DOES match the graybox) to build
a pin index keyed by the FUSED view's net ids instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from faultflow.project.aggregate import _wbc_pin_index, _wbc_pin_index_extest


def _graybox() -> dict[str, Any]:
    """soc_wbr_si -> u_a__wi (wbc_in) -> (glue INV) -> u_b__wo (wbc_out) ->
    soc_wbr_so. Net ids match the module docstring's real-world repro: FROM_SYS=4."""
    return {
        "creator": "test",
        "modules": {
            "soc_top": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "soc_wbr_se": {"direction": "input", "bits": [3]},
                    "soc_wbr_si": {"direction": "input", "bits": [100]},
                    "soc_wbr_so": {"direction": "output", "bits": [102]},
                    "add_sub": {"direction": "input", "bits": [4]},
                    "OUT": {"direction": "output", "bits": [11]},
                },
                "cells": {
                    "u_a__wi": {
                        "hide_name": 0,
                        "type": "$wbc_in_scan_faultflow",
                        "parameters": {},
                        "attributes": {
                            "faultflow_block": "blkA",
                            "faultflow_wbc": "__wi",
                        },
                        "port_directions": {
                            "CLK": "input",
                            "FROM_SYS": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_CORE": "output",
                            "CTO": "output",
                        },
                        "connections": {
                            "CLK": [2],
                            "FROM_SYS": [4],
                            "CTI": [100],
                            "SE": [3],
                            "TO_CORE": [20],
                            "CTO": [101],
                        },
                    },
                    "g_ic": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__inv_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [20], "Y": [21]},
                    },
                    "u_b__wo": {
                        "hide_name": 0,
                        "type": "$wbc_out_scan_faultflow",
                        "parameters": {},
                        "attributes": {
                            "faultflow_block": "blkB",
                            "faultflow_wbc": "__wo",
                        },
                        "port_directions": {
                            "CLK": "input",
                            "FROM_CORE": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_SYS": "output",
                            "CTO": "output",
                        },
                        "connections": {
                            "CLK": [2],
                            "FROM_CORE": [21],
                            "CTI": [101],
                            "SE": [3],
                            "TO_SYS": [11],
                            "CTO": [102],
                        },
                    },
                },
                "netnames": {},
            }
        },
    }


def _write(tmp: Path) -> Path:
    import json

    p = tmp / "graybox.json"
    p.write_text(json.dumps(_graybox(), indent=2), encoding="utf-8")
    return p


def test_extest_pin_index_uses_fused_net_ids_not_graybox_ones(tmp_path: Path) -> None:
    gpath = _write(tmp_path)
    graybox_pins = _wbc_pin_index(gpath, "soc_top")
    extest_pins = _wbc_pin_index_extest(gpath, "soc_top")

    # The graybox's own net 4 (FROM_SYS) resolves to blkA's __wi -- but the fused
    # EXTEST view will NOT have a fault at net 4 (fusion reallocates it), so the
    # graybox-keyed index alone is useless for a scan_extest scope's faults.
    assert graybox_pins[4] == ("blkA", "__wi", "FROM_SYS", "in")

    # The fused index must map SOME (fused-view) net id to the SAME canonical
    # (block, wbc, pin, side) identity for every WBC pin -- that's the contract
    # Guard 3 depends on for handoff completeness.
    values = set(extest_pins.values())
    assert ("blkA", "__wi", "FROM_SYS", "in") in values
    assert ("blkB", "__wo", "TO_SYS", "out") in values

    # And critically: the fused index's KEYS are NOT the graybox's net ids (127+ in
    # the real repro, definitely not 4/11) -- if this ever regresses to just
    # reusing graybox net ids, this assertion catches it via a real fusion run.
    assert 4 not in extest_pins or extest_pins[4] != ("blkA", "__wi", "FROM_SYS", "in")


def test_extest_pin_index_covers_the_outward_boundary_pins(tmp_path: Path) -> None:
    gpath = _write(tmp_path)
    extest_pins = _wbc_pin_index_extest(gpath, "soc_top")
    values = set(extest_pins.values())
    # Guard 3's handoff check only needs the OUTWARD (sys-facing) pins -- the ones
    # a block excludes as wbr_decoupled and the assembly must own: wbc_in's
    # FROM_SYS and wbc_out's TO_SYS. The core-facing pins (TO_CORE/FROM_CORE) are
    # block-owned regardless and, post-fusion, safed/dangling with no live fault
    # site to bridge -- fuse_wbr_into_view's port_map only carries `sys_net` for
    # this reason, and that is all this index needs to carry too.
    assert ("blkA", "__wi", "FROM_SYS", "in") in values
    assert ("blkB", "__wo", "TO_SYS", "out") in values
