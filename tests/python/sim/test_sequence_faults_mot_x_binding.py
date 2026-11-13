"""simulate_sequence_faults_mot_x: detection over multiple observation times. A
fault is detected when every completion of the unknowns (here the flops' power-up
values) shows a difference at some compared bit, perhaps a different one for each.
Fixture tests/cpp/fixtures/tiny_mot.json: h0 and h1 hold their power-up values,
Y = SEL ? A : h0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_mot.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
INPUTS = ["CLK", "RESET_B", "A", "SEL", "M"]


def _net(core: Any, port: str) -> int:
    (module,) = json.loads(FIXTURE.read_text(encoding="utf-8"))["modules"].values()
    (bit,) = module["ports"][port]["bits"]
    (index,) = [
        int(row["compiled_net_index"])
        for row in core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail", [])
        if row["site_key"] == f"net:{bit}:stem"
    ]
    return index


def _held(a_values: list[int]) -> tuple[list[list[bool]], list[bool]]:
    """Two cycles per value of A, CLK low (sampled) then high; SEL 1, M 0."""
    cycles: list[list[bool]] = []
    sample: list[bool] = []
    for a in a_values:
        cycles += [
            [False, True, a == 1, True, False],
            [True, True, a == 1, True, False],
        ]
        sample += [True, False]
    return cycles, sample


@pytest.mark.unit
def test_every_power_up_state_caught_somewhere_is_detected(
    require_cpp_core: None,
) -> None:
    """SEL stuck at 0 shows h0 at Y where A is expected: h0 = 1 fails at the first
    sample, h0 = 0 at the first A = 1. The single-sample X grade only possibly
    detects it."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    cycles, sample = _held([0, 0, 1, 0])
    faults = [(_net(core, "SEL"), 0)]
    single = core.simulate_sequence_faults_x(
        str(FIXTURE), str(CELL_MAP), INPUTS, cycles, sample, ["Y"], faults
    )
    assert single["first_definite"] == [-1]
    assert single["first_possible"] == [0]
    result = core.simulate_sequence_faults_mot_x(
        str(FIXTURE), str(CELL_MAP), INPUTS, cycles, sample, ["Y"], [], faults
    )
    assert result["golden"] == [[0], [0], [1], [0]]
    assert result["outcome"] == ["detected"]
    assert result["detected_by"] == [2]
    # Not comparing sample 2 leaves h0 = 0 escaping.
    compare = [[True], [True], [False], [True]]
    masked = core.simulate_sequence_faults_mot_x(
        str(FIXTURE),
        str(CELL_MAP),
        INPUTS,
        cycles,
        sample,
        ["Y"],
        compare,
        faults,
        witness=True,
    )
    assert masked["outcome"] == ["escapes"]
    assert masked["detected_by"] == [-1]
    # A power-up value for each of the three flops, all unknown (which flop powers
    # up how is checked in the core's own tests).
    (witness,) = masked["witness"]
    assert len(witness) == 3 and set(witness) <= {0, 1}


@pytest.mark.unit
def test_out_of_budget_is_undecided(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    cycles, sample = _held([0, 0, 1, 0])
    result = core.simulate_sequence_faults_mot_x(
        str(FIXTURE),
        str(CELL_MAP),
        INPUTS,
        cycles,
        sample,
        ["Y"],
        [],
        [(_net(core, "SEL"), 0)],
        max_variables=2,
    )
    assert result["outcome"] == ["undecided"]
    assert result["detected_by"] == [-1]
