"""The access path of the JTAG-owned non-scan logic (faultflow.scan.nonscan.jtag_sites,
rule 3): a net only the JTAG logic drives -- the TAP's own inputs, a TDR bit, and what
combinational logic makes of them and constants -- is test-access connectivity, tested
by ``ff.py jtag``, wherever it goes: its stem and each of its branches, down to the
D pin of the first flop on the other side. A gate that mixes in a core signal
is the core's: its output stays scan's, but the pin the JTAG side enters by is not.

The chip: u_tap__tdr (clk tck, D = tdi) drives g_buf, g_and (with the core input c)
and g_k (with a constant 1); g_buf's output feeds the scan flops u_a and u_b, g_and's
u_c, g_k's u_d. u_rst_sync__s0 -> s1 is a settled synchronizer (on clk, cleared by
rst_n held inactive) whose output feeds u_e and u_f: not the JTAG logic's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.nonscan import analyze_nonscan, jtag_sites

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"
TOP = "access"
TAP_INPUTS = ("tck", "tms", "tdi", "trst_n")
NONSCAN = ("u_tap__tdr", "u_tap__st", "u_rst_sync__s0", "u_rst_sync__s1")


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {
            p: "output" if p in ("Q", "X", "Y") else "input" for p in conns
        },
        "connections": {p: [n] for p, n in conns.items()},
    }


def _chip() -> dict[str, Any]:
    ports = {"clk": 2, "c": 3, "tck": 5, "trst_n": 6, "tdi": 7, "rst_n": 9}
    outputs = {"tdo": 8, "q_a": 40, "q_b": 41, "q_c": 42, "q_d": 43, "q_e": 44}
    outputs["q_f"] = 45
    buf, and2 = "sky130_fd_sc_hd__buf_1", "sky130_fd_sc_hd__and2_1"
    cells = {
        "u_tap__tdr": _cell(DFXTP, CLK=5, D=7, Q=20),
        "u_tap__st": _cell(DFRTP, CLK=5, D=7, RESET_B=6, Q=8),
        "g_buf": _cell(buf, A=20, X=21),
        "g_and": _cell(and2, A=20, B=3, X=30),
        "g_k": _cell(and2, A=20, B="1", X=31),
        "u_a": _cell(DFXTP, CLK=2, D=21, Q=40),
        "u_b": _cell(DFXTP, CLK=2, D=21, Q=41),
        "u_c": _cell(DFXTP, CLK=2, D=30, Q=42),
        "u_d": _cell(DFXTP, CLK=2, D=31, Q=43),
        "u_rst_sync__s0": _cell(DFRTP, CLK=2, D="1", RESET_B=9, Q=60),
        "u_rst_sync__s1": _cell(DFRTP, CLK=2, D=60, RESET_B=9, Q=61),
        "u_e": _cell(DFXTP, CLK=2, D=61, Q=44),
        "u_f": _cell(DFXTP, CLK=2, D=61, Q=45),
    }
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": {
                    **{
                        n: {"direction": "input", "bits": [b]} for n, b in ports.items()
                    },
                    **{
                        n: {"direction": "output", "bits": [b]}
                        for n, b in outputs.items()
                    },
                },
                "cells": cells,
                "netnames": {
                    name: {"hide_name": 0, "bits": [bit], "attributes": {}}
                    for name, bit in {**ports, **outputs}.items()
                },
            }
        }
    }


def _sites(tmp_path: Path, **kwargs: Any) -> tuple[set[str], set[str]]:
    """The JTAG sites of the chip, and all of its site keys."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    design = _chip()
    netlist = tmp_path / "chip.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    rows = list(core.list_site_keys(str(netlist), str(CELL_MAP_PATH), "fail", []))
    manifest = {
        "clock_nets": [2],
        "scan_inputs": ["scan_in_0"],
        "scan_enable": "scan_en",
        "ineligible_ffs": [
            {"instance": name, "cell_type": "x", "reason": "nonscan_policy"}
            for name in NONSCAN
        ],
    }
    module = design["modules"][TOP]
    setup = analyze_nonscan(
        module,
        manifest,
        CELL_MAP,
        globs=("u_tap__*", "u_rst_sync__*"),
        holds=[("trst_n", 0), ("tck", 0), ("rst_n", 1)],
        scan_reset_holds={},
    )
    sites = jtag_sites(rows, module, CELL_MAP, setup, tdo="tdo", **kwargs)
    return sites, {str(row["site_key"]) for row in rows}


@pytest.mark.unit
def test_what_only_the_jtag_logic_drives_is_access_connectivity(
    tmp_path: Path, require_cpp_core: None
) -> None:
    sites, keys = _sites(tmp_path, tap_inputs=TAP_INPUTS)
    # The TDR bit's net (20) fans out three ways: each branch is the access path's,
    # whatever the cell is (a buffer, an AND with the core's c, an AND with 1).
    assert {
        "net:20:stem",
        "net:20:branch:g_buf:A",
        "net:20:branch:g_and:A",
        "net:20:branch:g_k:A",
    } <= sites
    # A buffer of it (21) and an AND with a constant (31): driven by it alone, so
    # theirs too -- down to the D pin of the first scan flop.
    assert {
        "net:21:stem",
        "net:21:branch:u_a:D",
        "net:21:branch:u_b:D",
        "net:31:stem",
    } <= sites
    # The AND with c mixes in a core signal: its output (30), c and the scan flops
    # they feed are scan's.
    core = {"net:30:stem", "net:3:stem", "net:40:stem", "net:42:stem", "net:2:stem"}
    assert core <= keys and not core & sites


@pytest.mark.unit
def test_a_settled_flop_drives_no_access_path(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The reset synchronizer's output (61) feeds two scan flops: its branches
    into them are scan's -- the settled flop is no JTAG logic."""
    sites, keys = _sites(tmp_path, tap_inputs=TAP_INPUTS)
    settled = {"net:61:branch:u_e:D", "net:61:branch:u_f:D"}
    assert settled <= keys and not settled & sites
    assert not {key for key in sites if key.split(":")[1] in {"60", "61", "9"}}


@pytest.mark.unit
def test_without_the_tap_inputs_a_flops_nets_are_still_its_own_access_path(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The rule needs no TAP port names to find a TDR bit's path: only the TAP's
    own inputs, tdi among them, are added by name."""
    sites, _ = _sites(tmp_path)
    assert {"net:21:stem", "net:21:branch:u_a:D", "net:31:stem"} <= sites
