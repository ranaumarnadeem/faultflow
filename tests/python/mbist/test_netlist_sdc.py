"""The netlist SDC of the inserted crossings (faultflow.mbist.netlist_sdc): each
synchronizer the manifest names, found among the netlist's flops by its instance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.integrations.autombist import AutombistInstance, AutombistManifest
from faultflow.mbist.netlist_sdc import (
    NetlistCrossing,
    NetlistSdcError,
    netlist_crossings,
    netlist_sdc,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads((ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text("utf-8"))
SHELL = "g_bank[0].u_mem"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"

pytestmark = pytest.mark.unit


def _flop(**pins: int | str) -> dict[str, Any]:
    return {
        "type": DFRTP,
        "port_directions": {p: "output" if p == "Q" else "input" for p in pins},
        "connections": {p: [n] for p, n in pins.items()},
    }


def _module() -> dict[str, Any]:
    """The shell's reset synchronizer (D tied 1, cleared by rst_n) resets its
    test_mode synchronizer, whose first flop reads test_mode."""
    rst = f"{SHELL}__u_rst_sync__$auto$ff.cc:337:slice$"
    sync = f"{SHELL}__u_sync_test_mode__$auto$ff.cc:337:slice$"
    ports = {"clk": 2, "rst_n": 3, "test_mode": 4}
    return {
        "ports": {n: {"direction": "input", "bits": [b]} for n, b in ports.items()},
        "cells": {
            f"{rst}1": _flop(CLK=2, D="1", RESET_B=3, Q=10),
            f"{rst}2": _flop(CLK=2, D=10, RESET_B=3, Q=11),
            f"{sync}8": _flop(CLK=2, D=12, RESET_B=11, Q=13),
            f"{sync}7": _flop(CLK=2, D=4, RESET_B=11, Q=12),
        },
    }


def _manifest(*leaves: str) -> AutombistManifest:
    return AutombistManifest(
        top_module="chip_top",
        wrapper=Path("chip_top_mbist.v"),
        instances=tuple(
            AutombistInstance(
                category="mbist_shell",
                hierarchical_path=f"{SHELL}__{leaf}",
                hierarchy_hint="separate",
                instance_name=leaf,
                module_type=f"faultflow_mbist_{leaf}",
                sources=(),
            )
            for leaf in leaves
        ),
    )


def test_each_crossing_enters_its_synchronizer_by_its_own_pins() -> None:
    found = netlist_crossings(
        _module(), CELL_MAP, _manifest("u_rst_sync", "u_sync_test_mode")
    )
    rst = f"{SHELL}__u_rst_sync__$auto$ff.cc:337:slice$"
    assert found == [
        NetlistCrossing(
            (f"{rst}1/RESET_B", f"{rst}2/RESET_B"),
            f"{SHELL}__u_rst_sync: the chip reset, into clk",
        ),
        NetlistCrossing(
            (f"{SHELL}__u_sync_test_mode__$auto$ff.cc:337:slice$7/D",),
            f"{SHELL}__u_sync_test_mode: test_mode, into clk",
        ),
    ]
    sdc = netlist_sdc("chip_top_scan.v", found)
    assert "set_false_path -to [get_pins {g_bank\\[0\\].u_mem__u_sync_test_mode" in sdc


def test_a_synchronizer_with_no_flop_in_the_netlist_is_refused() -> None:
    with pytest.raises(NetlistSdcError, match="u_sync_bist_start"):
        netlist_crossings(_module(), CELL_MAP, _manifest("u_sync_bist_start"))


def test_a_prefix_names_them_inside_a_compressed_design() -> None:
    module = _module()
    module["cells"] = {f"core_inst__{k}": v for k, v in module["cells"].items()}
    (crossing,) = netlist_crossings(
        module, CELL_MAP, _manifest("u_sync_test_mode"), prefix="core_inst__"
    )
    assert crossing.pins[0].startswith("core_inst__g_bank[0]")
