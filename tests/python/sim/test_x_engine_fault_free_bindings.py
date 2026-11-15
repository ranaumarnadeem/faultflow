"""The X engine's fault-free outputs of a sequence and of a launch/capture pair.

fault_free_sequence_outputs_x is what scan-check's normal-mode comparison runs on
an X-mode design (a latch the two-valued engine does not model): every flop and
latch powered up 0 as in the two-valued one, so with no unknown it gives the
two-valued outputs. fault_free_pair_outputs_x is the good machine of a transition
pattern's capture frame, as the two-frame grade sees it: a latch the capture
closes keeps its launch-frame value.

tests/cpp/fixtures/tiny_xlatch.json: q = buf(dlxtp(D=d, GATE=g))."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LATCH = ROOT / "tests/cpp/fixtures/tiny_xlatch.json"
FLOPS = ROOT / "tests/cpp/fixtures/tiny_mot.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LATCH_INPUTS = ["d", "g", "rn"]


def _frame(d: bool, g: bool) -> dict[str, bool]:
    return {"d": d, "g": g, "rn": True}


@pytest.mark.unit
def test_a_latch_powers_up_0_unless_asked_unknown(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    closed = [[_frame(True, False)]]
    args = (str(LATCH), str(CELL_MAP))
    assert core.fault_free_sequence_outputs_x(*args, closed, LATCH_INPUTS, ["q"]) == [
        {"q": False}
    ]
    assert core.fault_free_sequence_outputs_x(
        *args, closed, LATCH_INPUTS, ["q"], initial_unknown=True
    ) == [{"q": None}]
    # Opened, then closed: it holds what it took.
    held = [[_frame(True, True), _frame(False, False)]]
    assert core.fault_free_sequence_outputs_x(
        *args, held, LATCH_INPUTS, ["q"], initial_unknown=True
    ) == [{"q": True}]


@pytest.mark.unit
def test_with_nothing_unknown_the_x_outputs_are_the_two_valued_ones(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    inputs = ["CLK", "RESET_B", "A", "SEL", "M"]
    outputs = ["Y", "Z", "W"]
    rng = random.Random(5)
    sequences = []
    for _ in range(40):
        sequence = []
        for _ in range(rng.randint(1, 6)):
            frame = {name: rng.random() < 0.5 for name in inputs}
            sequence += [{**frame, "CLK": False}, {**frame, "CLK": True}]
        sequences.append(sequence)
    args = (str(FLOPS), str(CELL_MAP), sequences, inputs, outputs)
    two_valued = core.fault_free_sequence_outputs(*args)
    x = core.fault_free_sequence_outputs_x(*args, "fail+x")
    assert x == two_valued
    assert all(value is not None for row in x for value in row.values())


@pytest.mark.unit
def test_the_capture_frame_keeps_what_the_launch_left_in_a_latch(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    args = (str(LATCH), str(CELL_MAP))
    launch, capture = _frame(True, True), _frame(False, False)
    # Alone the capture frame knows nothing of the latch; after the launch, its
    # value.
    assert core.fault_free_outputs_x(*args, [capture], LATCH_INPUTS, ["q"]) == [
        {"q": None}
    ]
    assert core.fault_free_pair_outputs_x(
        *args, launch, capture, LATCH_INPUTS, ["q"]
    ) == {"q": True}
    # A launch that leaves it closed leaves it as it powered up.
    assert core.fault_free_pair_outputs_x(
        *args, _frame(True, False), capture, LATCH_INPUTS, ["q"]
    ) == {"q": None}
    # The capture's unknown inputs: open to an unknown d, it reads X.
    assert core.fault_free_pair_outputs_x(
        *args,
        launch,
        _frame(False, True),
        LATCH_INPUTS,
        ["q"],
        capture_unknown=["d"],
    ) == {"q": None}
