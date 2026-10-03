"""The X-isolation proof (faultflow.jtag.xcheck) on small sky130 netlists: a TAP flop
t0 on tck/trst_n and a frozen flop f0 on clk/rst_n, and the variants it must refuse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.jtag.xcheck import ChipReset, JtagPorts, JtagSetupError, analyze
from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "jtag_tiny"
PORTS = {"tck": 2, "tms": 3, "tdi": 4, "trst_n": 5, "clk": 6, "rst_n": 7}


def _cell(cell_type: str, **conns: int | str) -> dict[str, Any]:
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


# --- the controlling-value rule, the chip reset's pulse, flops known by induction ---

CHIP_PORTS = {"tck": 2, "tms": 3, "tdi": 4, "trst_n": 5, "clk": 6, "rst_n": 7}


def _chip(tdo: int = 10, **cells: dict[str, Any]) -> dict[str, Any]:
    """A TAP flop t0 (output 10) on tck, cleared by trst_n, then the cells given;
    inputs as in CHIP_PORTS, plus en (8); tdo is net ``tdo``."""
    all_cells = {"t0": _cell("dfrtp_1", CLK=2, D=4, RESET_B=5, Q=10), **cells}
    inputs = {**CHIP_PORTS, "en": 8}
    ports = {
        name: {"direction": "input", "bits": [net]} for name, net in inputs.items()
    }
    ports["tdo"] = {"direction": "output", "bits": [tdo]}
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": ports,
                "cells": all_cells,
                "netnames": {
                    name: {"bits": port["bits"]} for name, port in ports.items()
                },
            }
        }
    }


def _shell(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A shell in miniature: s0 -> s1, a reset synchronizer on clk cleared by rst_n,
    resets c0, a collar flop on clk whose output a status TDR bit t1 captures to tdo
    (through o0)."""
    cells = {
        "s0": _cell("dfrtp_1", CLK=6, D="1", RESET_B=7, Q=20),
        "s1": _cell("dfrtp_1", CLK=6, D=20, RESET_B=7, Q=21),
        "c0": _cell("dfrtp_1", CLK=6, D=3, RESET_B=21, Q=22),
        "t1": _cell("dfrtp_1", CLK=2, D=22, RESET_B=5, Q=23),
        "o0": _cell("or2_1", A=10, B=23, X=24),
    }
    cells.update(overrides)
    return _chip(24, **cells)


def test_a_tap_flop_cleared_through_an_and_with_trst_n_is_forced(
    tmp_path, require_cpp_core
):
    """warptap's TDR clear, trst_n & clr_n: trst_n at 0 forces it, whatever the
    synchronizer's output; the reset path runs through the AND's trst_n input."""
    design = _chip(
        y0=_cell("dfrtp_1", CLK=2, D="1", RESET_B=11, Q=12),
        a0=_cell("and2_1", A=5, B=7, X=11),
        a1=_cell("and2_1", A=5, B=12, X=13),
        t2=_cell("dfrtp_1", CLK=2, D=4, RESET_B=13, Q=14),
    )
    setup = _analyze(tmp_path, design, chip_reset=ChipReset("rst_n", 0))
    assert set(setup.tap_flops) == {"t0", "y0", "t2"}
    assert {
        ("net:13:branch:t2:RESET_B", "sa1"),
        ("net:5:branch:a1:A", "sa1"),
        ("net:11:branch:y0:RESET_B", "sa1"),
        ("net:5:branch:a0:A", "sa1"),
    } <= setup.reset_path_faults
    # Neither the synchronizer's output nor the chip reset holds them: no release.
    assert not {key for key, _ in setup.reset_path_faults} & {
        "net:12:branch:a1:B",
        "net:7:branch:a0:B",
    }


def test_a_tap_flop_trst_n_doesnt_reach_is_refused(tmp_path, require_cpp_core):
    design = _chip(
        a1=_cell("and2_1", A=7, B=8, X=13),
        t2=_cell("dfrtp_1", CLK=2, D=4, RESET_B=13, Q=14),
    )
    with pytest.raises(JtagSetupError, match="TAP flop t2 isn't forced by trst_n"):
        _analyze(tmp_path, design, chip_reset=ChipReset("rst_n", 0))


def test_the_pulsed_chip_reset_and_the_flops_it_resets_are_known(
    tmp_path, require_cpp_core
):
    """The chip reset is pulsed, not held: s0 and s1 are known from it, and c0 from
    s1 -- a chain whose every link, stuck inactive, is left ungraded."""
    setup = _analyze(tmp_path, _shell(), chip_reset=ChipReset("rst_n", 0))
    assert setup.pulse == ChipReset("rst_n", 0)
    assert setup.holds == {"clk": 0, "en": 0}
    assert setup.frozen_known == ("s0", "s1", "c0")
    assert setup.x_sources == ()
    assert {
        ("net:7:stem", "sa1"),
        ("net:7:branch:s1:RESET_B", "sa1"),
        ("net:21:stem", "sa1"),
        ("net:21:branch:c0:RESET_B", "sa1"),
    } <= setup.reset_path_faults


def test_without_the_pulse_a_released_chip_reset_leaves_the_collar_unknown(
    tmp_path, require_cpp_core
):
    """The chip reset held inactive, as the control TDRs need: nothing resets the
    synchronizer, so the collar flop's X reaches tdo through the status TDR."""
    with pytest.raises(JtagSetupError, match="unknown value can reach tdo"):
        _analyze(tmp_path, _shell(), holds={"rst_n": 1})


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        # c0's clock gated by a TAP flop: it may toggle.
        (
            {
                "g0": _cell("or2_1", A=6, B=10, X=30),
                "c0": _cell("dfrtp_1", CLK=30, D=3, RESET_B=21, Q=22),
            },
            "unknown value can reach tdo",
        ),
        # c0 reset from logic the rule doesn't follow.
        (
            {
                "x0": _cell("xor2_1", A=21, B=8, X=30),
                "c0": _cell("dfrtp_1", CLK=6, D=3, RESET_B=30, Q=22),
            },
            "unknown value can reach tdo",
        ),
        # c0 cleared by s1, but its preset comes from a TAP flop.
        (
            {"c0": _cell("dfbbp_1", CLK=6, D=3, RESET_B=21, SET_B=10, Q=22)},
            "unknown value can reach tdo",
        ),
    ],
    ids=["clock_gated_by_logic", "reset_from_logic", "preset_from_logic"],
)
def test_a_collar_flop_the_rules_dont_know_is_an_x_source(
    overrides, match, tmp_path, require_cpp_core
):
    with pytest.raises(JtagSetupError, match=match):
        _analyze(tmp_path, _shell(**overrides), chip_reset=ChipReset("rst_n", 0))
    # Off tdo, the same flop is only an X source.
    quiet = _shell(**overrides, t1=_cell("dfrtp_1", CLK=2, D=4, RESET_B=5, Q=23))
    setup = _analyze(tmp_path, quiet, chip_reset=ChipReset("rst_n", 0))
    assert 22 in setup.x_sources and "c0" not in setup.frozen_known


def test_a_clock_a_held_input_gates_off_is_frozen(tmp_path, require_cpp_core):
    """clk & en with en held at 0 gives no edges: c0 is frozen, and known."""
    design = _shell(
        g0=_cell("and2_1", A=6, B=8, X=30),
        c0=_cell("dfrtp_1", CLK=30, D=3, RESET_B=21, Q=22),
    )
    setup = _analyze(tmp_path, design, chip_reset=ChipReset("rst_n", 0))
    assert "c0" in setup.frozen_known


@pytest.mark.parametrize(
    ("reset", "holds", "match"),
    [
        (ChipReset("rst_n", 0), {"rst_n": 1}, "pulses the chip reset rst_n"),
        (ChipReset("nope", 0), {}, "no single-bit input port 'nope'"),
        (ChipReset("tms", 0), {}, "is a TAP port"),
    ],
)
def test_a_chip_reset_that_cant_be_pulsed_is_refused(
    reset, holds, match, tmp_path, require_cpp_core
):
    with pytest.raises(JtagSetupError, match=match):
        _analyze(tmp_path, _shell(), holds=holds, chip_reset=reset)
