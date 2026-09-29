"""The X-isolation proof (faultflow.jtag.xcheck) on small sky130 netlists: a TAP flop
t0 on tck/trst_n and a frozen flop f0 on clk/rst_n, and the variants it must refuse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.jtag.xcheck import JtagPorts, JtagSetupError, analyze
from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "jtag_tiny"
PORTS = {"tck": 2, "tms": 3, "tdi": 4, "trst_n": 5, "clk": 6, "rst_n": 7}


def _cell(cell_type: str, **conns: int) -> dict[str, Any]:
    outputs = {"Q", "X", "Y"}
    return {
        "type": f"sky130_fd_sc_hd__{cell_type}",
        "port_directions": {p: ("output" if p in outputs else "input") for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _design(**overrides: dict[str, Any]) -> dict[str, Any]:
    """t0: dfrtp on tck, reset by trst_n, D = tdi. f0: dfrtp on clk, reset by rst_n,
    D = tms. tdo = t0.Q & f0.Q; out = f0.Q."""
    cells = {
        "t0": _cell("dfrtp_1", CLK=2, D=4, RESET_B=5, Q=8),
        "f0": _cell("dfrtp_1", CLK=6, D=3, RESET_B=7, Q=9),
        "g0": _cell("and2_1", A=8, B=9, X=10),
        "g1": _cell("buf_1", A=9, X=12),
    }
    cells.update(overrides)
    ports = {name: {"direction": "input", "bits": [net]} for name, net in PORTS.items()}
    ports["tdo"] = {"direction": "output", "bits": [10]}
    ports["out"] = {"direction": "output", "bits": [12]}
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": ports,
                "cells": cells,
                "netnames": {
                    name: {"bits": port["bits"]} for name, port in ports.items()
                },
            }
        }
    }


def _analyze(tmp_path: Path, design: dict[str, Any], **kwargs: Any):
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return analyze(
        _load_core(),
        path,
        CELL_MAP,
        TOP,
        ports=JtagPorts(),
        holds=kwargs.pop("holds", {}),
        unsupported=kwargs.pop("unsupported", "fail"),
        **kwargs,
    )


def test_flops_are_classified_and_the_holds_settled(tmp_path, require_cpp_core):
    setup = _analyze(tmp_path, _design())
    assert setup.tap_flops == ("t0",)
    assert setup.frozen_known == ("f0",)
    assert setup.x_sources == ()
    assert setup.holds == {"clk": 0, "rst_n": 0}
    assert setup.input_order == ("tck", "tms", "tdi", "trst_n", "clk", "rst_n")


def test_reset_paths_stuck_inactive_are_listed(tmp_path, require_cpp_core):
    """trst_n or rst_n stuck at 1 keeps its flop out of reset; stuck at 0 holds it in
    reset, which is deterministic and stays graded."""
    faults = _analyze(tmp_path, _design()).reset_path_faults
    assert {
        ("net:5:stem", "sa1"),
        ("net:5:branch:t0:RESET_B", "sa1"),
        ("net:7:stem", "sa1"),
        ("net:7:branch:f0:RESET_B", "sa1"),
    } == faults


def test_an_active_high_reset_is_held_at_1(tmp_path, require_cpp_core):
    """rst drives f0's RESET_B through an inverter: held at 1, and the inactive stuck-at
    flips across the inverter."""
    setup = _analyze(
        tmp_path,
        _design(
            f0=_cell("dfrtp_1", CLK=6, D=3, RESET_B=13, Q=9),
            i0=_cell("inv_1", A=7, Y=13),
        ),
    )
    assert setup.holds["rst_n"] == 1
    assert {("net:7:stem", "sa0"), ("net:13:stem", "sa1")} <= setup.reset_path_faults


def test_an_inverted_tck_still_clocks_a_tap_flop(tmp_path, require_cpp_core):
    setup = _analyze(
        tmp_path,
        _design(
            t0=_cell("dfrtp_1", CLK=11, D=4, RESET_B=5, Q=8),
            i0=_cell("inv_1", A=2, Y=11),
        ),
    )
    assert setup.tap_flops == ("t0",)


def test_an_unreset_frozen_flop_off_tdo_is_only_an_x_source(tmp_path, require_cpp_core):
    setup = _analyze(
        tmp_path,
        _design(
            f0=_cell("dfxtp_1", CLK=6, D=3, Q=9),
            g0=_cell("buf_1", A=8, X=10),
        ),
    )
    assert setup.x_sources == (9,)
    assert setup.frozen_known == ()


@pytest.mark.parametrize(
    ("overrides", "holds", "match"),
    [
        (
            {"t0": _cell("dfrtp_1", CLK=2, D=4, RESET_B=7, Q=8)},
            {},
            "isn't forced by trst_n",
        ),
        ({"f0": _cell("dfxtp_1", CLK=6, D=3, Q=9)}, {}, "unknown value can reach tdo"),
        # The unknown value crosses a TAP flop on its way to tdo.
        (
            {
                "f0": _cell("dfxtp_1", CLK=6, D=3, Q=9),
                "t0": _cell("dfrtp_1", CLK=2, D=9, RESET_B=5, Q=8),
                "g0": _cell("buf_1", A=8, X=10),
            },
            {},
            "unknown value can reach tdo",
        ),
        (
            {"f0": _cell("dfrtp_1", CLK=3, D=3, RESET_B=7, Q=9)},
            {},
            "neither tck nor a held",
        ),
        ({}, {"tms": 0}, "can't be held"),
        ({}, {"nope": 0}, "doesn't have"),
    ],
    ids=[
        "tap_flop_not_reset_by_trst",
        "unreset_flop_reaches_tdo",
        "x_through_a_tap_flop",
        "clock_from_tms",
        "holding_a_driven_port",
        "unknown_hold",
    ],
)
def test_setups_that_cant_be_graded_exactly_are_refused(
    overrides, holds, match, tmp_path, require_cpp_core
):
    with pytest.raises(JtagSetupError, match=match):
        _analyze(tmp_path, _design(**overrides), holds=holds)


def test_a_held_reset_released_by_an_explicit_hold_is_an_x_source(
    tmp_path, require_cpp_core
):
    """Holding rst_n at 1 releases f0's reset: f0 is unknown, and reaches tdo."""
    with pytest.raises(JtagSetupError, match="unknown value can reach tdo"):
        _analyze(tmp_path, _design(), holds={"rst_n": 1})


def test_a_blackbox_output_reaching_tdo_is_refused(tmp_path, require_cpp_core):
    design = _design(g0=_cell("and2_1", A=8, B=14, X=10))
    design["modules"][TOP]["cells"]["mem"] = {
        "type": "fake_mem",
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [3], "Y": [14]},
    }
    with pytest.raises(JtagSetupError, match="unknown value can reach tdo"):
        _analyze(tmp_path, design, unsupported="blackbox", blackbox_instances=["mem"])
