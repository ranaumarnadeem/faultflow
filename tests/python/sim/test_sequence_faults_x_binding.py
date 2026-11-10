"""simulate_sequence_faults_x: sequence grading with unknown values -- flops
powered up X, inputs X where asked, and each fault's detections split into
definite (a 0 against a 1) and possible (known against X).
Fixture tests/cpp/fixtures/tiny_seq_observe.json: DIN -> u0 -> u1 -> u2 -> DOUT
(dfrtp, RESET_B), EXTRA = ~u0.Q."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_seq_observe.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
INPUTS = ["CLK", "RESET_B", "DIN"]


def _shift(reset_periods: int) -> tuple[list[list[bool]], list[bool]]:
    """Two cycles per period, CLK low (sampled) then high, RESET_B low for the
    first `reset_periods`."""
    cycles: list[list[bool]] = []
    sample: list[bool] = []
    for period, din in enumerate((0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0)):
        reset_b = period >= reset_periods
        cycles += [[False, reset_b, din == 1], [True, reset_b, din == 1]]
        sample += [True, False]
    return cycles, sample


def _net(core: Any, port: str) -> int:
    (module,) = json.loads(FIXTURE.read_text(encoding="utf-8"))["modules"].values()
    (bit,) = module["ports"][port]["bits"]
    (index,) = [
        int(row["compiled_net_index"])
        for row in core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail", [])
        if row["site_key"] == f"net:{bit}:stem"
    ]
    return index


@pytest.mark.unit
def test_with_nothing_unknown_definite_is_the_two_valued_detection(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    cycles, sample = _shift(2)
    nets = sorted(
        {
            int(row["compiled_net_index"])
            for row in core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail", [])
        }
    )
    faults = [(net, kind) for net in nets for kind in (0, 1)]
    args = (
        str(FIXTURE),
        str(CELL_MAP),
        INPUTS,
        cycles,
        sample,
        ["DOUT", "EXTRA"],
        faults,
    )
    two = core.simulate_sequence_faults(*args)
    x = core.simulate_sequence_faults_x(*args, initial_unknown=False)
    assert x["first_definite"] == two["first_sample"]
    assert x["first_possible"] == [-1] * len(faults)
    assert x["golden"] == [[int(v) for v in row] for row in two["golden"]]


@pytest.mark.unit
def test_power_up_unknown_and_unknown_inputs(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    cycles, sample = _shift(0)  # no reset: the flops start unknown
    din = _net(core, "DIN")
    result = core.simulate_sequence_faults_x(
        str(FIXTURE), str(CELL_MAP), INPUTS, cycles, sample, ["DOUT"], [(din, 0)]
    )
    dout = [row[0] for row in result["golden"]]
    assert dout[:3] == [None, None, None]  # three flops from DIN
    assert dout[3] == 0
    assert result["first_definite"] == [5]

    # A reset stuck inactive, on a design that only the reset makes known: the
    # faulty machine stays X, so the fault is possibly detected, never definitely.
    cycles, sample = _shift(2)
    reset_b = _net(core, "RESET_B")
    din_unknown = [[False, False, True] for _ in cycles]
    result = core.simulate_sequence_faults_x(
        str(FIXTURE),
        str(CELL_MAP),
        INPUTS,
        cycles,
        sample,
        ["DOUT"],
        [(reset_b, 1)],
        unknown=din_unknown,
    )
    assert result["golden"][0] == [0]
    # The unknown DIN reaches DOUT three edges after the reset ends.
    assert result["golden"][4] == [0]
    assert result["golden"][5] == [None]
    assert result["first_definite"] == [-1]
    assert result["first_possible"] == [0]
