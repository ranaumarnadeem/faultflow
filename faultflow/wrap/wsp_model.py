"""A behavioural model of the IEEE 1500 wrapper serial port (WSP) that
:mod:`faultflow.wrap.wsp` inserts -- the reference its netlist is checked against,
cycle by cycle.

The wrapper's registers:

- the instruction register (WIR): a 3-bit shift stage, WSI into bit 0 and bit 2 out
  to WSO, which shifts on the rising edge of WRCK while SelectWIR and ShiftWR are 1;
  and an update stage, the instruction in force, which takes the shift stage on the
  falling edge of WRCK while SelectWIR and UpdateWR are 1. WRSTN at 0 clears the
  update stage at once: WS_BYPASS, the functional mode.
- the bypass register (WBY): one bit, WSI to WSO, which shifts while ShiftWR is 1
  and captures 0 while CaptureWR is 1, the WIR not selected; cleared by WRSTN.
- the boundary register (WBR): the boundary cells (:mod:`faultflow.wrap.cell`),
  WSI through them to WSO in serial order, in a serial instruction.

Instructions (:data:`OPCODES`, the update stage's bits, bit 0 first shifted last):
WS_BYPASS 000, WS_EXTEST 001, WP_INTEST 010, WP_EXTEST 011, WS_INTEST 100; any other
code is WS_BYPASS.

- In a parallel instruction (WP_*) the cells' hold and safe are INTEST's or EXTEST's
  (cell.spec), and the WBR shifts on the wrapper chains' own ports with the scan
  enable, not on the WSP.
- In a serial one (WS_EXTEST, WS_INTEST) the cells take the mode too, but every cell
  holds, its flop keeping its value, unless the WIR is deselected and CaptureWR is 1
  -- then the cells that observe in the mode capture -- or ShiftWR is 1, when the WBR
  shifts from WSI. A cell holds through the WIR's own shifts and updates (IEEE 1500
  WBR rule: no change without an event of its own).
- WSO: the WIR's last bit when SelectWIR is 1, else the WBR's last cell in a serial
  instruction, else WBY.

A step is one tester cycle: the inputs applied, WSO strobed, then WRCK pulses (rises,
then falls) when the cycle says so.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Sequence

from faultflow.wrap.cell import INPUT, SIDES

OPCODES = {
    "WS_BYPASS": 0b000,
    "WS_EXTEST": 0b001,
    "WP_INTEST": 0b010,
    "WP_EXTEST": 0b011,
    "WS_INTEST": 0b100,
}
WIR_BITS = 3
SERIAL = ("WS_EXTEST", "WS_INTEST")


def instruction(code: int) -> str:
    """The instruction an update stage of ``code`` puts in force."""
    for name, value in OPCODES.items():
        if value == code:
            return name
    return "WS_BYPASS"


def mode_of(name: str) -> tuple[int, int]:
    """The (INTEST, EXTEST) mode an instruction puts the cells in."""
    if name in ("WP_INTEST", "WS_INTEST"):
        return 1, 0
    if name in ("WP_EXTEST", "WS_EXTEST"):
        return 0, 1
    return 0, 0


@dataclass(frozen=True)
class WspCycle:
    """One tester cycle on the WSP: each signal's value (WRCK aside), whether WRCK
    pulses, and each boundary cell's functional input (its port bit, or the core's
    value), in serial order."""

    wrstn: int = 1
    selectwir: int = 0
    shiftwr: int = 0
    capturewr: int = 0
    updatewr: int = 0
    wsi: int | None = 0
    pulse: bool = True
    cfi: tuple[int | None, ...] = ()


@dataclass
class WspModel:
    """The wrapper's state: ``sides`` are the boundary cells' (input or output), in
    the WBR's serial order, WSI first. Every register starts unknown (None) but what
    WRSTN clears."""

    sides: Sequence[str]
    shift: list[int | None] = field(default_factory=lambda: [None] * WIR_BITS)
    update: list[int | None] = field(default_factory=lambda: [None] * WIR_BITS)
    wby: int | None = None
    wbr: list[int | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        if any(side not in SIDES for side in self.sides):
            raise ValueError(f"a boundary cell's side is one of {SIDES}")
        if not self.wbr:
            self.wbr = [None] * len(self.sides)

    @property
    def instruction(self) -> str | None:
        """The instruction in force, or None while the update stage is unknown."""
        if any(bit is None for bit in self.update):
            return None
        code = sum(int(bit or 0) << k for k, bit in enumerate(self.update))
        return instruction(code)

    def _serial(self) -> bool | None:
        name = self.instruction
        return None if name is None else name in SERIAL

    def wso(self, cycle: WspCycle) -> int | None:
        """WSO at the strobe of ``cycle`` (before its edge)."""
        self._reset(cycle)
        if cycle.selectwir:
            return self.shift[WIR_BITS - 1]
        serial = self._serial()
        if serial is None:
            return None
        if serial:
            return self.wbr[-1] if self.wbr else None
        return self.wby

    def outputs(self, cycle: WspCycle) -> list[int | None]:
        """Each boundary cell's functional output in ``cycle`` (to the core for an
        input cell, to the port for an output cell): 0 where the mode makes it safe,
        its flop where the mode holds it, else its functional input. A serial
        instruction's extra holds don't show: they hold only cells the mode keeps
        safe."""
        self._reset(cycle)
        name = self.instruction
        if name is None:
            return [None] * len(self.sides)
        intest, extest = mode_of(name)
        found: list[int | None] = []
        for k, side in enumerate(self.sides):
            hold, safe = (intest, extest) if side == INPUT else (extest, intest)
            if safe:
                found.append(0)
            elif hold:
                found.append(self.wbr[k])
            else:
                found.append(cycle.cfi[k] if k < len(cycle.cfi) else None)
        return found

    def _hold_safe(
        self, side: str, intest: int, extest: int, name: str, cycle: WspCycle
    ) -> tuple[int, int]:
        hold, safe = (intest, extest) if side == INPUT else (extest, intest)
        if name in SERIAL:
            capturing = cycle.capturewr and not cycle.selectwir
            hold = int(hold or not capturing)
        return hold, safe

    def _reset(self, cycle: WspCycle) -> None:
        if not cycle.wrstn:
            self.update = [0] * WIR_BITS
            self.wby = 0

    def step(self, cycle: WspCycle) -> int | None:
        """Apply ``cycle``: WSO at its strobe, returned; then its WRCK pulse."""
        strobed = self.wso(cycle)
        if not cycle.pulse:
            return strobed
        name = self.instruction
        # The rising edge: the WIR's shift stage, WBY and the WBR.
        shift: list[int | None] = list(self.shift)
        if cycle.selectwir and cycle.shiftwr:
            shift = [cycle.wsi, *shift[:-1]]
        wby = self.wby
        if not cycle.selectwir and cycle.shiftwr:
            wby = cycle.wsi
        elif not cycle.selectwir and cycle.capturewr:
            wby = 0
        self.wbr = self._wbr_edge(name, cycle)
        self.shift, self.wby = shift, wby
        # The falling edge: the WIR's update stage.
        if cycle.selectwir and cycle.updatewr:
            self.update = list(self.shift)
        self._reset(cycle)
        return strobed

    def _wbr_edge(self, name: str | None, cycle: WspCycle) -> list[int | None]:
        if name is None:
            return [None] * len(self.wbr)
        serial = name in SERIAL
        if serial and cycle.shiftwr and not cycle.selectwir:
            return [cycle.wsi, *self.wbr[:-1]]
        intest, extest = mode_of(name)
        found: list[int | None] = []
        for k, side in enumerate(self.sides):
            hold, _ = self._hold_safe(side, intest, extest, name, cycle)
            if hold:
                found.append(self.wbr[k])
            else:
                found.append(cycle.cfi[k] if k < len(cycle.cfi) else None)
        return found


def program(name: str | int) -> list[WspCycle]:
    """The cycles that put an instruction in force from any state -- ``name``, or
    any 3-bit code: WRSTN pulsed, the code shifted into the WIR (bit 2 first), then
    updated, then the WSP idle (WIR deselected)."""
    code = OPCODES[name] if isinstance(name, str) else int(name)
    cycles = [WspCycle(wrstn=0, pulse=False)]
    for k in reversed(range(WIR_BITS)):
        cycles.append(WspCycle(selectwir=1, shiftwr=1, wsi=(code >> k) & 1))
    cycles.append(WspCycle(selectwir=1, updatewr=1))
    cycles.append(WspCycle(pulse=False))
    return cycles


def model_run(
    sides: Sequence[str], cycles: Sequence[WspCycle]
) -> tuple[list[int | None], WspModel]:
    """WSO at each cycle's strobe, and the model after the cycles."""
    model = WspModel(sides)
    return [model.step(cycle) for cycle in cycles], model


class WspChainModel:
    """Models of wrappers whose WSI and WSO are chained -- WSI into the first, each
    WSO into the next WSI, the last one's WSO the chain's -- and whose other WSP
    controls are the same signals, as a SoC's blocks have them. ``sides``: each
    wrapper's boundary cells' sides, in its serial order, the first wrapper's first."""

    def __init__(self, sides: Sequence[Sequence[str]]) -> None:
        if not sides:
            raise ValueError("a chain needs a wrapper")
        self.models = [WspModel(list(each)) for each in sides]

    def wso(self, cycle: WspCycle) -> int | None:
        """The chain's WSO at the strobe of ``cycle``, without applying it."""
        return self.models[-1].wso(cycle)

    def step(self, cycle: WspCycle) -> int | None:
        """Apply ``cycle`` to every wrapper; the chain's WSO at its strobe. Every WSO
        is read before any edge, so each wrapper shifts in what its neighbour showed."""
        strobes = [model.wso(cycle) for model in self.models]
        feeds = [cycle.wsi, *strobes[:-1]]
        for model, feed in zip(self.models, feeds):
            model.step(dataclasses.replace(cycle, wsi=feed))
        return strobes[-1]
