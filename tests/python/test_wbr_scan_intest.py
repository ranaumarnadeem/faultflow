"""INTEST + internal-scan fusion (faultflow/scan/wbr_view.py).

A wrapped sequential core, after scan reduction, has its flip-flops exposed as
`__ppi_*`/`__ppo_*` pseudo-ports AND still carries its IEEE 1500 boundary cells.
`fuse_wbr_into_view` additionally exposes the wrapper boundary: each `wbc_in`
core net becomes a controllable pseudo-PI `__wbi_*`, each `wbc_out` core net an
observed pseudo-PO `__wbo_*`, and the system-side top ports are decoupled.

The fixture below models that scan-reduced wrapped view directly:

    IN(sys) -> wbc_in0 -> core_in -INV(g0)-> n1 -+-> NAND2(g1) -> core_out
                                                 |                   |
                          __ppi_ff0 (FF state) -/    $ffobserve_ff0 -+-> __ppo_ff0
                                                          wbc_out0 -> OUT(sys)

After INTEST fusion the controllable inputs are {__wbi_wbc_in0 (core_in),
__ppi_ff0} and the observed outputs are {__wbo_wbc_out0 (core_out), __ppo_ff0
(n1, the FF D-cone) }. n1 sits *behind* the FF — observable only because the FF
data is a scan pseudo-PO; core_in is controllable only via the wrapper boundary.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from faultflow.scan.wbr_view import WBI_PREFIX, WBO_PREFIX, fuse_wbr_into_view

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/osu/osu035.json"
TOP = "core_intest"


def _cell(ctype: str, conns: dict[str, list[int]], dirs: dict[str, str]) -> dict:
    return {
        "hide_name": 0,
        "type": ctype,
        "parameters": {},
        "attributes": {},
        "port_directions": dirs,
        "connections": conns,
    }


def _reduced_wrapped_view() -> dict:
    """A scan-reduced wrapped core (FFs already -> __ppi_/__ppo_)."""
    return {
        "creator": "test fixture",
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "IN": {"direction": "input", "bits": [2]},
                    "__ppi_ff0": {"direction": "input", "bits": [5]},
                    "OUT": {"direction": "output", "bits": [7]},
                    "__ppo_ff0": {"direction": "output", "bits": [8]},
                },
                "cells": {
                    "wbc_in0": _cell(
                        "$wbc_in_faultflow",
                        {"FROM_SYS": [2], "TO_CORE": [3]},
                        {"FROM_SYS": "input", "TO_CORE": "output"},
                    ),
                    "g0": _cell(
                        "INVX1",
                        {"A": [3], "Y": [4]},
                        {"A": "input", "Y": "output"},
                    ),
                    "g1": _cell(
                        "NAND2X1",
                        {"A": [4], "B": [5], "Y": [6]},
                        {"A": "input", "B": "input", "Y": "output"},
                    ),
                    "wbc_out0": _cell(
                        "$wbc_out_faultflow",
                        {"FROM_CORE": [6], "TO_SYS": [7]},
                        {"FROM_CORE": "input", "TO_SYS": "output"},
                    ),
                    "$ffobserve_ff0": _cell(
                        "$faultflow_observe_buf",
                        {"A": [4], "Y": [8]},
                        {"A": "input", "Y": "output"},
                    ),
                },
                "netnames": {
                    "IN": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "core_in": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "n1": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "__ppi_ff0": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "core_out": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "OUT": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "__ppo_ff0": {"hide_name": 0, "bits": [8], "attributes": {}},
                },
            }
        },
    }


def test_fuse_exposes_wbr_boundary_as_pseudo_ports() -> None:
    fused, port_map = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "intest")
    mod = fused["modules"][TOP]
    ports = mod["ports"]

    wbi = f"{WBI_PREFIX}wbc_in0"
    wbo = f"{WBO_PREFIX}wbc_out0"

    # WBR boundary became pseudo-PI / pseudo-PO on the CORE nets.
    assert ports[wbi]["direction"] == "input"
    assert ports[wbi]["bits"] == [3]  # core_in net
    assert ports[wbo]["direction"] == "output"

    # Scan pseudo-ports coexist (the fusion is additive).
    assert "__ppi_ff0" in ports
    assert "__ppo_ff0" in ports

    # System-side top ports are decoupled in INTEST.
    assert "IN" not in ports
    assert "OUT" not in ports

    # Wrapper cells removed; an observe buffer taps the core output.
    assert "wbc_in0" not in mod["cells"]
    assert "wbc_out0" not in mod["cells"]
    assert "$wbobserve_wbc_out0" in mod["cells"]

    assert port_map["wbc_in0"]["role"] == "ppi"
    assert port_map["wbc_in0"]["core_net"] == 3
    assert port_map["wbc_out0"]["role"] == "ppo"


def test_functional_mode_is_noop() -> None:
    fused, port_map = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "functional")
    assert port_map == {}
    assert "IN" in fused["modules"][TOP]["ports"]  # untouched


@pytest.mark.golden
def test_intest_fused_view_simulates_core_boundary(require_cpp_core: None) -> None:
    """Driving the core via __wbi and observing core nets via __wbo/__ppo proves
    the fused INTEST view tests sequential internals through the boundary, with
    the system-side top ports gone."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    fused, _ = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "intest")

    ins = [f"{WBI_PREFIX}wbc_in0", "__ppi_ff0"]
    outs = ["__ppo_ff0", f"{WBO_PREFIX}wbc_out0"]
    # n1 = ~core_in ; core_out = ~(n1 & ff_state)
    vectors = [
        {f"{WBI_PREFIX}wbc_in0": False, "__ppi_ff0": True},  # n1=1, core_out=0
        {f"{WBI_PREFIX}wbc_in0": True, "__ppi_ff0": True},  # n1=0, core_out=1
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fused.json"
        path.write_text(json.dumps(fused))
        res = list(
            core.fault_free_outputs(
                str(path), str(CELL_MAP), vectors, ins, outs, "fail"
            )
        )

    r0, r1 = dict(res[0]), dict(res[1])
    assert bool(r0["__ppo_ff0"]) is True
    assert bool(r0[f"{WBO_PREFIX}wbc_out0"]) is False
    assert bool(r1["__ppo_ff0"]) is False
    assert bool(r1[f"{WBO_PREFIX}wbc_out0"]) is True
