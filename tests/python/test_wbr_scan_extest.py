"""EXTEST + internal-scan fusion (faultflow/scan/wbr_view.py).

Symmetric with test_wbr_scan_intest.py but for EXTEST: the *interconnect*/boundary
is tested while the core is held dead/safe. The fixture below models a scan-reduced
wrapped core with a small piece of *interconnect* logic on each system side:

    IN_SYS -INV(g_ic)-> sys_in -> wbc_in0(FROM_SYS) ; wbc_in0(TO_CORE) -> core_in
    core_in -INV(g0)-> n1 -NAND2(g1, B=__ppi_ff0)-> core_out
    core_out -> wbc_out0(FROM_CORE) ; wbc_out0(TO_SYS) -> OUT_SYS
    $ffobserve_ff0: A=n1 -> __ppo_ff0

After EXTEST fusion:
  * wbc_in0 system net (sys_in) is OBSERVED -> __wbi_obs_wbc_in0; core_in tied 0.
  * wbc_out0 system net (OUT_SYS) is CONTROLLED -> input __wbo_ctl_wbc_out0; the
    core net (core_out) is left dangling.
  * __ppo_ff0 (the scan observe of core-internal n1) is DROPPED; __ppi_ff0 stays.
  * Top-level PIs/POs (IN_SYS, OUT_SYS) are KEPT.

Hand-computed EXTEST behaviour:
  * __wbi_obs_wbc_in0 == ~IN_SYS              (interconnect INV g_ic)
  * OUT_SYS           == __wbo_ctl_wbc_out0   (control drives the system PO net)
  * n1 (core internal) is in NO observable cone (its __ppo_ is gone).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from faultflow.scan.wbr_view import (
    WBI_OBS_PREFIX,
    WBO_CTL_PREFIX,
    fuse_wbr_into_view,
)

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/osu/osu035.json"
TOP = "core_extest"


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
    """A scan-reduced wrapped core with interconnect logic on each system side.

    Net ids: IN_SYS=2 sys_in=9 core_in=3 n1=4 __ppi_ff0=5 core_out=6 OUT_SYS=7
             __ppo_ff0=8.
    """
    return {
        "creator": "test fixture",
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "IN_SYS": {"direction": "input", "bits": [2]},
                    "__ppi_ff0": {"direction": "input", "bits": [5]},
                    "OUT_SYS": {"direction": "output", "bits": [7]},
                    "__ppo_ff0": {"direction": "output", "bits": [8]},
                },
                "cells": {
                    "g_ic": _cell(
                        "INVX1",
                        {"A": [2], "Y": [9]},
                        {"A": "input", "Y": "output"},
                    ),
                    "wbc_in0": _cell(
                        "$wbc_in_faultflow",
                        {"FROM_SYS": [9], "TO_CORE": [3]},
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
                    "IN_SYS": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "sys_in": {"hide_name": 0, "bits": [9], "attributes": {}},
                    "core_in": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "n1": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "__ppi_ff0": {
                        "hide_name": 0,
                        "bits": [5],
                        "attributes": {},
                    },
                    "core_out": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "OUT_SYS": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "__ppo_ff0": {
                        "hide_name": 0,
                        "bits": [8],
                        "attributes": {},
                    },
                },
            }
        },
    }


def test_extest_fuse_exposes_interconnect_and_safes_core() -> None:
    fused, port_map = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "extest")
    mod = fused["modules"][TOP]
    ports = mod["ports"]
    cells = mod["cells"]

    obs = f"{WBI_OBS_PREFIX}wbc_in0"
    ctl = f"{WBO_CTL_PREFIX}wbc_out0"

    # System side of wbc_out0 becomes a controllable INPUT.
    assert ctl in ports
    assert ports[ctl]["direction"] == "input"
    assert ports[ctl]["bits"] == [7]  # former OUT_SYS / TO_SYS net

    # System side of wbc_in0 becomes an observed OUTPUT.
    assert obs in ports
    assert ports[obs]["direction"] == "output"

    # Wrapper cells are removed.
    assert "wbc_in0" not in cells
    assert "wbc_out0" not in cells

    # The former core-input net (core_in == net 3) is driven by a const-0 cell.
    safe_cells = [
        c
        for c in cells.values()
        if isinstance(c, dict)
        and c.get("connections", {}).get("Y") == [3]
        and c.get("connections", {}).get("A") == ["0"]
    ]
    assert len(safe_cells) == 1
    assert safe_cells[0]["type"] == "$faultflow_observe_buf"

    # __ppo_ scan observe ports + their observe buffer are dropped (dead core).
    assert "__ppo_ff0" not in ports
    assert "$ffobserve_ff0" not in cells

    # __ppi_ inputs are kept; top-level PIs/POs are kept.
    assert "__ppi_ff0" in ports
    assert "IN_SYS" in ports
    assert "OUT_SYS" in ports

    # Port-map roles: wbc_out control => ppi; wbc_in observe => ppo.
    assert port_map["wbc_out0"]["role"] == "ppi"
    assert port_map["wbc_out0"]["port"] == ctl
    assert port_map["wbc_out0"]["sys_net"] == 7
    assert port_map["wbc_in0"]["role"] == "ppo"
    assert port_map["wbc_in0"]["port"] == obs
    assert port_map["wbc_in0"]["core_net"] == 3
    assert port_map["wbc_in0"]["sys_net"] == 9


def test_extest_schema_ver_attribute_set() -> None:
    fused, _ = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "extest")
    attrs = fused["modules"][TOP]["attributes"]
    assert attrs["faultflow_wbr_scan_view_schema_ver"] == "wbr-scan-extest-1"


@pytest.mark.golden
def test_extest_fused_view_simulates_interconnect(require_cpp_core: None) -> None:
    """Drive the interconnect via top PI + __wbo_ctl_*, observe via __wbi_obs_*
    and top PO, and prove the dead core's internal net is NOT observable (its
    __ppo_ is gone)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    fused, _ = fuse_wbr_into_view(_reduced_wrapped_view(), TOP, "extest")

    obs = f"{WBI_OBS_PREFIX}wbc_in0"
    ctl = f"{WBO_CTL_PREFIX}wbc_out0"

    # Drive top PI (IN_SYS) and the system-side control input (__wbo_ctl).
    # Observe the system-side tap (__wbi_obs == ~IN_SYS) and the top PO
    # (OUT_SYS == __wbo_ctl). __ppo_ff0 must NOT be a valid observable anymore.
    ins = ["IN_SYS", ctl, "__ppi_ff0"]
    outs = [obs, "OUT_SYS"]
    vectors = [
        {"IN_SYS": False, ctl: True, "__ppi_ff0": False},  # obs=1, OUT=1
        {"IN_SYS": True, ctl: False, "__ppi_ff0": True},  # obs=0, OUT=0
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
    # __wbi_obs_wbc_in0 == ~IN_SYS  (interconnect inverter g_ic).
    assert bool(r0[obs]) is True
    assert bool(r1[obs]) is False
    # OUT_SYS == __wbo_ctl_wbc_out0 (control drives the system PO net directly).
    assert bool(r0["OUT_SYS"]) is True
    assert bool(r1["OUT_SYS"]) is False
    # The core-internal net n1 has no observable: its __ppo_ port is gone, so
    # requesting it as an output must fail (it is not a valid observable).
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fused2.json"
        path.write_text(json.dumps(fused))
        with pytest.raises(Exception):
            list(
                core.fault_free_outputs(
                    str(path),
                    str(CELL_MAP),
                    vectors,
                    ins,
                    ["__ppo_ff0"],
                    "fail",
                )
            )
