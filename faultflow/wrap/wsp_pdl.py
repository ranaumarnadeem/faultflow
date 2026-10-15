"""The test setup program of an IEEE 1500 WSP mode (faultflow.wrap.wsp_setup), checked
against warptap's PDL.

A scan test of a wrapper its WSP controls starts from a program that loads the mode's
instruction into the WIR (:func:`faultflow.wrap.wsp_setup.mode_program`, which
faultflow writes from its own instruction table). The same step in PDL is three
statements -- ``iReset``, ``iWrite WIR <instruction>``, ``iApply`` -- and warptap's
interpreter retargets them onto the wrapper serial port (:func:`setup_pdl`,
:func:`warptap_mode_program`). The two programs are derived independently from the
wrapper's instruction table, the one warptap reads from the ICL and the record's own
opcodes: :func:`check_setup_program` requires them equal, cycle by cycle -- the chip
inputs at each cycle and the WRCK pulses -- so a WIR bit order, a reset or update cycle
or an instruction code the two read differently is refused before a test runs on it.
"""

from __future__ import annotations

from typing import Any, Mapping

from faultflow.scan.setup import SetupCycle
from faultflow.wrap.wsp_icl import _warptap, soc_design, wrapper_model
from faultflow.wrap.wsp_program import WspProgramError, is_soc
from faultflow.wrap.wsp_setup import (
    HELD,
    MODE_INSTRUCTIONS,
    _levels,
    access_of,
    mode_program,
)


def _interpreter(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    mode: str,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> tuple[Any, Any, Any]:
    """warptap's PDL interpreter after the mode's setup statements, its module and
    the wrapper model."""
    if mode not in MODE_INSTRUCTIONS:
        raise WspProgramError(f"no wrapper mode {mode!r}")
    pdl = _warptap("pdl_1500")
    instruction = MODE_INSTRUCTIONS[mode]
    if is_soc(wrapper):
        # Every WIR of the chain written, one scan loading them all.
        model = soc_design(wrapper, top=top)
        interp = pdl.Wsp1500Interpreter(model)
        interp.iReset()
        for k in range(len(model.wrappers)):
            interp.iWrite(f"u{k}.WIR", instruction)
    else:
        model = wrapper_model(
            wrapper, scan_outs, top=top, scan_ins=scan_ins, scan_enable=scan_enable
        )
        interp = pdl.Wsp1500Interpreter(model)
        interp.iReset()
        interp.iWrite("WIR", instruction)
    interp.iApply()
    return interp, pdl, model


def setup_pdl(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    mode: str,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> str:
    """The PDL that puts the block in ``mode``'s instruction."""
    interp, pdl, model = _interpreter(
        wrapper,
        scan_outs,
        mode=mode,
        top=top,
        scan_ins=scan_ins,
        scan_enable=scan_enable,
    )
    return str(pdl.to_pdl_1500(interp.history, model))


def warptap_mode_program(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    mode: str,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> tuple[SetupCycle, ...]:
    """The setup program warptap's PDL retargets to, as faultflow's cycles on the
    chip's inputs."""
    interp, _, _ = _interpreter(
        wrapper,
        scan_outs,
        mode=mode,
        top=top,
        scan_ins=scan_ins,
        scan_enable=scan_enable,
    )
    rows = _warptap("wsp_play").to_wsp_rows(interp.program)
    access = access_of(wrapper)
    return tuple(
        SetupCycle(
            _levels(access, **{role: int(getattr(row.cycle, role)) for role in HELD}),
            (access.wrck,) if row.cycle.pulse else (),
        )
        for row in rows
    )


def check_setup_program(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    mode: str,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> str:
    """Require faultflow's setup program for ``mode`` to equal warptap's PDL retarget;
    the PDL, if it does."""
    ours = mode_program(wrapper, mode)
    theirs = warptap_mode_program(
        wrapper,
        scan_outs,
        mode=mode,
        top=top,
        scan_ins=scan_ins,
        scan_enable=scan_enable,
    )
    if ours != theirs:
        first = next(
            (k for k, (a, b) in enumerate(zip(ours, theirs)) if a != b),
            min(len(ours), len(theirs)),
        )
        raise WspProgramError(
            f"the {mode} setup program disagrees with warptap's PDL retarget of "
            f"'iWrite WIR {MODE_INSTRUCTIONS[mode]}' ({len(ours)} cycles against "
            f"{len(theirs)}; the first difference is at cycle {first})"
        )
    return setup_pdl(
        wrapper,
        scan_outs,
        mode=mode,
        top=top,
        scan_ins=scan_ins,
        scan_enable=scan_enable,
    )
