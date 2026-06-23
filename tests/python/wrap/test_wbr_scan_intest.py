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

from faultflow.scan.wbr_view import (
    WBI_PREFIX,
    WBO_PREFIX,
    build_wbr_generic_name_map,
    extract_wbr_cells,
    fuse_wbr_into_view,
)

ROOT = Path(__file__).resolve().parents[3]
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


def _scan_model_wrapped_view() -> dict:
    """A scan-reduced wrapped core whose boundary uses the NATIVE SHIFTABLE
    scan-model cells (`$wbc_*_scan_faultflow`) and a wrapper scan chain
    (wbr_si -> __wi_D -> __wo_Q -> wbr_so).

        D(sys 3) -> __wi_D -TO_CORE 9-> INV g0 -FROM_CORE 12-> __wo_Q -> Q(sys 6)
        wrapper chain: wbr_si 7 -> __wi_D.CTO 13 -> __wo_Q.CTI 13 -> wbr_so 14
    """
    return {
        "creator": "test fixture",
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [6]},
                    "wbr_si": {"direction": "input", "bits": [7]},
                    "wbr_se": {"direction": "input", "bits": [8]},
                    "wbr_so": {"direction": "output", "bits": [14]},
                },
                "cells": {
                    "__wi_D": _cell(
                        "$wbc_in_scan_faultflow",
                        {
                            "CLK": [2],
                            "FROM_SYS": [3],
                            "CTI": [7],
                            "SE": [8],
                            "TO_CORE": [9],
                            "CTO": [13],
                        },
                        {
                            "CLK": "input",
                            "FROM_SYS": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_CORE": "output",
                            "CTO": "output",
                        },
                    ),
                    "g0": _cell(
                        "INVX1", {"A": [9], "Y": [12]}, {"A": "input", "Y": "output"}
                    ),
                    "__wo_Q": _cell(
                        "$wbc_out_scan_faultflow",
                        {
                            "CLK": [2],
                            "FROM_CORE": [12],
                            "CTI": [13],
                            "SE": [8],
                            "TO_SYS": [6],
                            "CTO": [14],
                        },
                        {
                            "CLK": "input",
                            "FROM_CORE": "input",
                            "CTI": "input",
                            "SE": "input",
                            "TO_SYS": "output",
                            "CTO": "output",
                        },
                    ),
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "wbr_si": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "wbr_se": {"hide_name": 0, "bits": [8], "attributes": {}},
                    "__core_D": {"hide_name": 0, "bits": [9], "attributes": {}},
                    "core_q": {"hide_name": 0, "bits": [12], "attributes": {}},
                    "wbr_chain": {"hide_name": 0, "bits": [13], "attributes": {}},
                    "wbr_so": {"hide_name": 0, "bits": [14], "attributes": {}},
                },
            }
        },
    }


def test_extract_wbr_cells_matches_scan_model_with_chain_nets() -> None:
    mod = _scan_model_wrapped_view()["modules"][TOP]
    recs = {r.instance: r for r in extract_wbr_cells(mod)}

    assert set(recs) == {"__wi_D", "__wo_Q"}
    assert recs["__wi_D"].is_input is True
    assert recs["__wi_D"].core_net == 9  # TO_CORE
    assert recs["__wi_D"].sys_net == 3  # FROM_SYS
    assert set(recs["__wi_D"].chain_nets) == {7, 8, 13}  # CTI, SE, CTO
    assert recs["__wo_Q"].is_input is False
    assert recs["__wo_Q"].core_net == 12  # FROM_CORE
    assert recs["__wo_Q"].sys_net == 6  # TO_SYS
    assert set(recs["__wo_Q"].chain_nets) == {13, 8, 14}  # CTI, SE, CTO


def test_fuse_scan_model_wbr_reduces_and_drops_chain_ports() -> None:
    fused, port_map = fuse_wbr_into_view(_scan_model_wrapped_view(), TOP, "intest")
    mod = fused["modules"][TOP]
    ports = mod["ports"]
    cells = mod["cells"]

    # Core boundary nets became controllable / observed pseudo-ports.
    assert ports[f"{WBI_PREFIX}__wi_D"]["bits"] == [9]
    assert ports[f"{WBI_PREFIX}__wi_D"]["direction"] == "input"
    assert ports[f"{WBO_PREFIX}__wo_Q"]["direction"] == "output"

    # Both scan-model WBR cells are gone -> the view is combinational (no FF/wbc).
    assert "__wi_D" not in cells
    assert "__wo_Q" not in cells
    assert not any("wbc" in str(c.get("type", "")) for c in cells.values())
    assert "g0" in cells  # core logic preserved

    # System-side ports decoupled AND the dangling wrapper scan-chain ports
    # (wbr_si / wbr_se / wbr_so) dropped.
    for dropped in ("D", "Q", "wbr_si", "wbr_se", "wbr_so"):
        assert dropped not in ports, f"{dropped} should be dropped"

    assert port_map["__wi_D"]["role"] == "ppi"
    assert port_map["__wo_Q"]["role"] == "ppo"


def test_functional_mode_is_noop() -> None:
    fused, port_map = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "functional")
    assert port_map == {}
    assert "IN" in fused["modules"][TOP]["ports"]  # untouched


def test_build_wbr_generic_name_map_intest() -> None:
    """The fused boundary ports map back to the generic top-port names, and the
    system-side nets are collected as decoupled (so they leave the denominator)."""
    # name mapping is computed against the *generic* (pre-fusion) netlist, while
    # the port map comes from fusing a separate copy.
    generic = _reduced_wrapped_view()
    _, port_map = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "intest")

    stim, obs, decoupled = build_wbr_generic_name_map(generic, TOP, port_map, "intest")

    assert stim == {f"{WBI_PREFIX}wbc_in0": "IN"}
    assert obs == {f"{WBO_PREFIX}wbc_out0": "OUT"}
    assert decoupled == {2, 7}  # IN net + OUT net are decoupled in INTEST


def test_build_wbr_generic_name_map_functional_is_empty() -> None:
    generic = _reduced_wrapped_view()
    stim, obs, decoupled = build_wbr_generic_name_map(generic, TOP, {}, "functional")
    assert stim == {}
    assert obs == {}
    assert decoupled == set()


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
