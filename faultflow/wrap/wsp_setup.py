"""How a scan test puts a wrapper its IEEE 1500 WSP controls (``[wrap] control =
wsp``, :mod:`faultflow.wrap.wsp`) in a test mode: the test setup program
(:mod:`faultflow.scan.setup`) that loads the mode's instruction into the WIR, and
the WSP inputs the test then holds.

A scan test runs with a parallel instruction in force -- WP_INTEST for INTEST,
WP_EXTEST for EXTEST -- or WS_BYPASS, the functional mode, for the functional
test (:data:`MODE_INSTRUCTIONS`). The program (:func:`program`) is
:func:`faultflow.wrap.wsp_model.program`'s, on the chip's inputs: WRSTN at 0
(WS_BYPASS, WBY at 0); the instruction shifted into the WIR, bit 2 first, a WRCK
pulse each; then updated, on the falling edge of one more. The test then holds
WRSTN at 1 and SelectWIR, ShiftWR, CaptureWR, UpdateWR and WSI at 0
(:func:`holds`): WRCK pulses with the scan clock, the WIR and WBY keep their
values -- the programmed class of :mod:`faultflow.scan.nonscan` proves it from
the program -- and the decode keeps the boundary cells in the mode.

A SoC's blocks share one WSP (:mod:`faultflow.project.soc_wrapper`): its inputs
reach every block's WSP inputs through buffers and inverters, and the blocks'
WSI and WSO form one chain. The program then shifts every block's instruction,
the last block's first, and updates them all at once.

Every cycle of the program names every WSP input: one an inverter drives would
otherwise sit at 1 behind an unnamed (so 0) chip input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.scan.setup import SetupCycle
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES, WIR_BITS

MODE_INSTRUCTIONS = {
    "functional": "WS_BYPASS",
    "intest": "WP_INTEST",
    "extest": "WP_EXTEST",
}
# The WSP inputs a program and a test set, beside WRCK: each one's level while
# the test runs.
HELD = {
    "wrstn": 1,
    "selectwir": 0,
    "shiftwr": 0,
    "capturewr": 0,
    "updatewr": 0,
    "wsi": 0,
}


@dataclass(frozen=True)
class WspAccess:
    """Where a tester drives a WSP, or a SoC's chain of them: for each WSP input of
    :data:`HELD`, the chip input bit and whether the way to the WSP inverts it;
    the scan clock that is WRCK; and how many WIRs the WSI-WSO chain holds."""

    pins: Mapping[str, tuple[str, bool]]
    wrck: str
    wirs: int = 1


def access_of(wrapper: Mapping[str, Any]) -> WspAccess:
    """The WSP access of a scan manifest's wrapper record, a block's
    (faultflow.wrap.record) or a SoC's (faultflow.project.soc_wrapper)."""
    if wrapper.get("control") != "wsp":
        raise WrapError("the wrapper's mode is set by its mode pins, not a WSP")
    wsp = wrapper["wsp"]
    if "blocks" in wrapper:
        pins = {
            role: (str(pin["port"]), bool(pin["inverted"]))
            for role, pin in wsp["pins"].items()
        }
        return WspAccess(pins, str(wsp["wrck"]), len(wsp["chain"]))
    ports = wsp["ports"]
    pins = {role: (str(ports[role]["port"]), False) for role in HELD}
    return WspAccess(pins, str(ports["wrck"]["port"]))


def _levels(access: WspAccess, **at_wsp: int) -> tuple[tuple[str, int], ...]:
    """Each WSP input's chip input at the value that puts the WSP's at
    ``at_wsp`` (every one named; one :data:`HELD` names but ``at_wsp`` doesn't at
    0)."""
    if set(access.pins) != set(HELD):
        raise WrapError(f"a WSP's inputs are {', '.join(HELD)}")
    values: dict[str, int] = {}
    for role, (name, inverted) in access.pins.items():
        value = int(at_wsp.get(role, 0)) ^ int(inverted)
        if values.setdefault(name, value) != value:
            raise WrapError(
                f"chip input {name} drives WSP inputs that need it at 0 and at 1"
            )
    return tuple(sorted(values.items()))


def holds(access: WspAccess) -> dict[str, int]:
    """The chip inputs a scan test holds, and their values (:data:`HELD` at the
    WSPs)."""
    return dict(_levels(access, **HELD))


def program(access: WspAccess, codes: Sequence[int]) -> tuple[SetupCycle, ...]:
    """The program that puts ``codes`` in force -- each WIR's, the one next to WSI
    first: WRSTN low, every code shifted in (the last WIR's first, each bit 2
    first), then the update."""
    if len(codes) != access.wirs:
        raise WrapError(f"{len(codes)} instruction(s) for {access.wirs} WIR(s)")
    cycles = [SetupCycle(_levels(access, wrstn=0))]
    for code in reversed(codes):
        if not 0 <= code < 1 << WIR_BITS:
            raise WrapError(f"no {WIR_BITS}-bit instruction {code}")
        for k in reversed(range(WIR_BITS)):
            bit = (code >> k) & 1
            levels = _levels(access, wrstn=1, selectwir=1, shiftwr=1, wsi=bit)
            cycles.append(SetupCycle(levels, (access.wrck,)))
    levels = _levels(access, wrstn=1, selectwir=1, updatewr=1)
    cycles.append(SetupCycle(levels, (access.wrck,)))
    return tuple(cycles)


def mode_program(wrapper: Mapping[str, Any], mode: str) -> tuple[SetupCycle, ...]:
    """The program that puts every WIR of ``wrapper`` (a scan manifest's wrapper
    record) in ``mode``'s instruction."""
    if mode not in MODE_INSTRUCTIONS:
        raise WrapError(f"no wrapper mode {mode!r}")
    access = access_of(wrapper)
    code = OPCODES[MODE_INSTRUCTIONS[mode]]
    return program(access, [code] * access.wirs)
