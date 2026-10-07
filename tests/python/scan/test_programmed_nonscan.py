"""Programmed non-scan flops: a test-mode register the test setup program
([scan] test_setup) loads, which then holds its value through the scan test. Scan ties
it there, and leaves to JTAG the stuck-ats that could let it go during the test and
those that change what the program loads.

The chip: u_cfg__q, the mode bit, loads din through the mux u_cfg__m when en is 1,
else keeps its value; clocked by clk, the scan clock, or by tck, a clock of its own. r0,
the one scan flop, captures d & mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key
from faultflow.scan.errors import ScanError
from faultflow.scan.nonscan import analyze_nonscan
from faultflow.scan.setup import SetupCycle

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads(
    (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
)
TOP = "mode_chip"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
MUX = "sky130_fd_sc_hd__mux2_1"
SCAN = "$scanff_faultflow"
# clk 2, tck 3, en 4, din 5, d 6, scan_en 7, scan_in 8; mode 10, mux out 11.
PORTS = {"clk": 2, "tck": 3, "en": 4, "din": 5, "d": 6, "scan_en": 7, "scan_in": 8}


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


def _chip(clock: int = 2) -> dict[str, Any]:
    cells = {
        "u_cfg__m": _cell(MUX, A0=10, A1=5, S=4, X=11),
        "u_cfg__q": _cell(DFXTP, CLK=clock, D=11, Q=10),
        "g_and": _cell("sky130_fd_sc_hd__and2_1", A=6, B=10, X=12),
        "r0": _cell(SCAN, CLK=2, D=12, SDI=8, SE=7, Q=13),
    }
    ports = {n: {"direction": "input", "bits": [b]} for n, b in PORTS.items()}
    ports["scan_out"] = {"direction": "output", "bits": [13]}
    return {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": {},
    }


MANIFEST = {
    "clock_nets": [2],
    "scan_inputs": ["scan_in"],
    "scan_enable": "scan_en",
    "ineligible_ffs": [
        {"instance": "u_cfg__q", "cell_type": DFXTP, "reason": "nonscan_policy"}
    ],
}


def _cycle(pulse: bool = False, **inputs: int) -> SetupCycle:
    return SetupCycle(tuple(sorted(inputs.items())), ("clk",) if pulse else ())


def _analyze(
    module: dict[str, Any], holds: dict[str, int], setup: tuple[SetupCycle, ...]
) -> Any:
    return analyze_nonscan(
        module,
        MANIFEST,
        CELL_MAP,
        globs=("u_cfg__*",),
        holds=sorted(holds.items()),
        scan_reset_holds={},
        setup=setup,
    )


def _stem(net: int) -> str:
    return canonical_site_key(SiteProvenance(yosys_net_id=net, kind="stem"))


@pytest.mark.unit
def test_a_register_the_program_loads_and_a_mux_holds(require_cpp_core: None) -> None:
    setup = _analyze(_chip(), {"en": 0}, (_cycle(True, en=1, din=1),))
    [flop] = setup.flops
    assert (flop.instance, flop.value, flop.programmed, flop.settle) == (
        "u_cfg__q",
        1,
        True,
        0,
    )
    assert setup.programmed == (flop,) and setup.x_sources == ()
    assert setup.owns("u_cfg__q") and setup.owns("u_cfg__m")
    # en at 1 in the test lets din in: en's stuck-at 1 releases it. en stuck at 0
    # and din stuck at 0 change what the program loads; din stuck at 1 doesn't.
    assert {
        (_stem(4), "sa1"),
        (_stem(4), "sa0"),
        (_stem(5), "sa0"),
    } <= setup.release_faults
    assert (_stem(5), "sa1") not in setup.release_faults


@pytest.mark.unit
def test_a_register_on_a_held_clock_of_its_own(require_cpp_core: None) -> None:
    # tck, a level, rises with en and din at 1; held at 0 in the test (falling into
    # it: the flop takes nothing on a falling edge), en free.
    program = (_cycle(en=1, din=1), _cycle(en=1, din=1, tck=1))
    setup = _analyze(_chip(clock=3), {"tck": 0}, program)
    assert [(f.value, f.programmed) for f in setup.flops] == [(1, True)]
    # Held at 1 instead, its value still holds -- no edge.
    setup = _analyze(_chip(clock=3), {"tck": 1}, program)
    assert [(f.value, f.programmed) for f in setup.flops] == [(1, True)]


@pytest.mark.unit
def test_what_the_program_leaves_unknown_or_free_is_refused(
    require_cpp_core: None,
) -> None:
    # No program: on the scan clock, its D its own output -- nothing settles it.
    with pytest.raises(ScanError, match="test setup program"):
        _analyze(_chip(), {"en": 0}, ())
    # A program that doesn't load it: its value is the power-up's.
    with pytest.raises(ScanError, match="test setup program"):
        _analyze(_chip(), {"en": 0}, (_cycle(True, en=0, din=1),))
    # Loaded, but en free in the test: din could load it.
    with pytest.raises(ScanError, match="test setup program"):
        _analyze(_chip(), {}, (_cycle(True, en=1, din=1),))


@pytest.mark.unit
def test_a_program_naming_anything_but_inputs_is_refused() -> None:
    with pytest.raises(ScanError, match="not an input of the chip"):
        _analyze(_chip(), {"en": 0}, (_cycle(True, en=1, nope=1),))
    with pytest.raises(ScanError, match="is a scan clock"):
        _analyze(_chip(), {"en": 0}, (_cycle(en=1, clk=1),))
    with pytest.raises(ScanError, match="not a scan clock"):
        _analyze(_chip(), {"en": 0}, (SetupCycle((("en", 1),), ("tck",)),))
