"""The integrity program of an IEEE 1500 WSP (faultflow.wrap.wsp): tester cycles
played on the WSP alone, each with what its behavioural model
(faultflow.wrap.wsp_model) expects at WSO, at the wrapped outputs and at the
wrapper chains' scan-outs -- what ``ff.py wsp`` grades the WSP's own faults with,
the faults every scan test leaves to it (``excluded_wsp``).

The program, test by test (:func:`integrity_program`):

- ``reset``: WRSTN low: WS_BYPASS, WBY cleared.
- ``wir_shift``: a pattern through the WIR's shift stage, SelectWIR and ShiftWR
  high: WSO is the bit that went in three shifts before.
- per instruction code, all eight (``code_<bits>``): the code loaded (WRSTN, the
  shift, the update), then the data register it selects shifted with a pattern
  -- WBY, one bit, for WS_BYPASS, the parallel instructions and the unused codes;
  the boundary register from WSI to WSO through every wrapper chain for WS_EXTEST
  and WS_INTEST -- then a capture and a shift-out: WBY captures 0, and in
  WS_EXTEST the input cells capture the ports, which the program drives with a
  pattern.
- ``held_update``: a code shifted in, then UpdateWR with SelectWIR low: the
  instruction in force stays.

Every input but the WSP's and the wrapped data inputs is 0 throughout: the scan
enable, the core's clocks, its resets. What the model can't know -- the core's
outputs an output cell captures in WS_INTEST, any register before its first load
-- is no care.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.wrap.cell import INPUT
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import (
    OPCODES,
    SERIAL,
    WIR_BITS,
    WspCycle,
    WspModel,
    instruction,
    program,
)


@dataclass(frozen=True)
class WspStep:
    """A tester cycle of the program: the WSP's inputs (``cycle``), the wrapped
    data inputs' values by label (input cells' ports; any other at 0), the test
    it belongs to, the WSO the model expects at its strobe (None: no care), and
    what it expects of the other outputs there: each wrapped output port, by
    label -- 0 where the instruction makes it safe, its cell's flop where it
    holds -- and each wrapper chain's scan-out, its last cell's flop."""

    cycle: WspCycle
    ports: tuple[tuple[str, int], ...]
    test: str
    wso: int | None
    expected: tuple[tuple[str, int | None], ...] = ()


def serial_cells(wrapper: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A block's boundary cells in the boundary register's serial order: wrapper
    chain by chain (faultflow.wrap.wsp.link_wsp strings them in index order), each
    from its head."""
    return sorted(wrapper["cells"], key=lambda c: (int(c["chain"]), int(c["position"])))


def _pattern(length: int, seed: int) -> list[int]:
    """`length` bits of a 4-bit LFSR's sequence from `seed`: both values and both
    transitions within a few bits."""
    state = 0b1011 ^ (seed & 0b1111) or 0b1011
    bits: list[int] = []
    for _ in range(length):
        bits.append(state & 1)
        feedback = ((state >> 3) ^ (state >> 2)) & 1
        state = ((state << 1) | feedback) & 0b1111
    return bits


def integrity_program(
    wrapper: Mapping[str, Any], scan_outs: Mapping[int, str] | None = None
) -> list[WspStep]:
    """The integrity program (see the module docstring) of the WSP a block's
    wrapper record (faultflow.wrap.record, control "wsp") names. `scan_outs`:
    each wrapper chain's scan-out port, by chain index, to compare too."""
    if wrapper.get("control") != "wsp" or "blocks" in wrapper:
        raise WrapError("ff.py wsp grades a block's IEEE 1500 WSP")
    cells = serial_cells(wrapper)
    sides = [str(cell["side"]) for cell in cells]
    labels = [str(cell["label"]) for cell in cells]
    inputs = [label for side, label in zip(sides, labels) if side == INPUT]
    tails = {
        port: max(
            (k for k, cell in enumerate(cells) if int(cell["chain"]) == chain),
            default=None,
        )
        for chain, port in (scan_outs or {}).items()
    }
    model = WspModel(sides)
    steps: list[WspStep] = []

    def run(test: str, cycle: WspCycle, ports: Mapping[str, int] | None = None) -> None:
        given = {label: int((ports or {}).get(label, 0)) for label in inputs}
        cfi = tuple(
            given[label] if side == INPUT else None
            for side, label in zip(sides, labels)
        )
        full = dataclasses.replace(cycle, cfi=cfi)
        out = model.outputs(full)
        expected = [
            (label, out[k])
            for k, (side, label) in enumerate(zip(sides, labels))
            if side != INPUT
        ]
        expected += [
            (port, None if tail is None else model.wbr[tail])
            for port, tail in tails.items()
        ]
        wso = model.step(full)
        steps.append(
            WspStep(cycle, tuple(sorted(given.items())), test, wso, tuple(expected))
        )

    def load(test: str, code: int) -> None:
        for cycle in program(code):
            run(test, cycle)

    run("reset", WspCycle(wrstn=0, pulse=False))
    for bit in _pattern(WIR_BITS + 8, 1):
        run("wir_shift", WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    for code in range(1 << WIR_BITS):
        test = f"code_{code:0{WIR_BITS}b}"
        load(test, code)
        serial = instruction(code) in SERIAL
        length = len(cells) if serial else 1
        for bit in _pattern(length + 2, code + 2):
            run(test, WspCycle(shiftwr=1, wsi=bit))
        captured = {
            label: bit for label, bit in zip(inputs, _pattern(len(inputs), code))
        }
        run(test, WspCycle(capturewr=1), captured)
        for bit in _pattern(length + 1, code + 3):
            run(test, WspCycle(shiftwr=1, wsi=bit))
    # UpdateWR with SelectWIR low updates nothing: WS_EXTEST stays in force.
    load("held_update", OPCODES["WS_EXTEST"])
    for bit in reversed([0, 1, 0]):  # WP_INTEST, shifted in
        run("held_update", WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    run("held_update", WspCycle(updatewr=1))
    for bit in _pattern(len(cells) + 1, 9):
        run("held_update", WspCycle(shiftwr=1, wsi=bit))
    if model.instruction != "WS_EXTEST":
        raise WrapError("the integrity program's held update changed the instruction")
    return steps


def program_tests(steps: Sequence[WspStep]) -> list[str]:
    """The program's tests, in order."""
    seen: list[str] = []
    for step in steps:
        if not seen or seen[-1] != step.test:
            seen.append(step.test)
    return seen
