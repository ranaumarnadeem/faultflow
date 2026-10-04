"""simulate_sequence_faults' fault_active: per cycle, whether the faults are
injected -- a scan protocol replay keeps them out of the preamble and the load.
Fixture tests/cpp/fixtures/tiny_seq_observe.json: DIN -> u0 -> u1 -> u2 -> DOUT."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_seq_observe.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _shift() -> tuple[list[list[bool]], list[bool]]:
    """Two cycles per period, CLK low (sampled) then high; the reset holds the
    first two periods. DIN's first 1 is latched at period 2's edge and reaches
    DOUT at sample 5."""
    cycles: list[list[bool]] = []
    sample: list[bool] = []
    periods = [(False, False), (False, True)] + [
        (True, din == 1) for din in (1, 0, 1, 1, 0, 0, 1, 0, 0, 0)
    ]
    for reset_b, din in periods:
        cycles += [[False, reset_b, din], [True, reset_b, din]]
        sample += [True, False]
    return cycles, sample


@pytest.mark.unit
def test_fault_active_limits_injection_to_the_given_cycles(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    (module,) = json.loads(FIXTURE.read_text(encoding="utf-8"))["modules"].values()
    (din_net,) = module["ports"]["DIN"]["bits"]
    (din,) = [
        int(row["compiled_net_index"])
        for row in core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail", [])
        if row["site_key"] == f"net:{din_net}:stem"
    ]
    cycles, sample = _shift()

    def first(active: list[bool] | None) -> int:
        extra = {} if active is None else {"fault_active": active}
        result = core.simulate_sequence_faults(
            str(FIXTURE),
            str(CELL_MAP),
            ["CLK", "RESET_B", "DIN"],
            cycles,
            sample,
            ["DOUT"],
            [(din, 0)],
            **extra,
        )
        return int(result["first_sample"][0])

    assert first(None) == 5
    assert first([True] * len(cycles)) == 5
    assert first([False] * len(cycles)) == -1
    only_period_2 = [cycle // 2 == 2 for cycle in range(len(cycles))]
    assert first(only_period_2) == 5
