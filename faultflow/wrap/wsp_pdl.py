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

from typing import Any, Mapping, Sequence

from faultflow.scan.setup import SetupCycle
from faultflow.wrap.wsp_icl import _warptap, soc_design, wrapper_model
from faultflow.wrap.wsp_program import (
    WspProgramError,
    is_soc,
    serial_cells,
    soc_blocks,
)
from faultflow.wrap.wsp_setup import (
    HELD,
    MODE_INSTRUCTIONS,
    _levels,
    access_of,
    instruction_program,
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
    instruction: str | None = None,
) -> tuple[Any, Any, Any]:
    """warptap's PDL interpreter after the mode's setup statements -- or
    ``instruction``'s, written into every WIR -- its module and the wrapper model."""
    if instruction is None:
        if mode not in MODE_INSTRUCTIONS:
            raise WspProgramError(f"no wrapper mode {mode!r}")
        instruction = MODE_INSTRUCTIONS[mode]
    pdl = _warptap("pdl_1500")
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


def _wbr_registers(wrapper: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """warptap's boundary registers in the order a data scan runs through them, WSI
    first -- a block's ``WBR<k>`` (its wrapper chains, by chain), or each SoC
    block's ``u<i>.WBR<k>`` in the WSP chain's order -- each with its cells' flops
    from its head, as the wrapper record names them."""
    if not is_soc(wrapper):
        cells = serial_cells(wrapper)
        chains = sorted({int(c["chain"]) for c in cells})
        return [
            (f"WBR{k}", [str(c["ff"]) for c in cells if int(c["chain"]) == chain])
            for k, chain in enumerate(chains)
        ]
    found: list[tuple[str, list[str]]] = []
    for index, (instance, _block, _chains) in enumerate(soc_blocks(wrapper)):
        cells = [c for c in wrapper["cells"] if str(c.get("instance")) == instance]
        chains = sorted({int(c["block_chain"]) for c in cells})
        for k, chain in enumerate(chains):
            own = sorted(
                (c for c in cells if int(c["block_chain"]) == chain),
                key=lambda c: int(c["block_position"]),
            )
            found.append((f"u{index}.WBR{k}", [str(c["ff"]) for c in own]))
    return found


def check_serial_loads(
    wrapper: Mapping[str, Any],
    serial: Mapping[str, Any],
    patterns: Sequence[Mapping[str, Any]],
    scan_outs: Mapping[int, str],
    *,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> str:
    """WS_EXTEST against warptap's PDL retarget (faultflow.wrap.serial): the setup
    program, ``iWrite WIR WS_EXTEST``'s; the serial chain the scan manifest
    ``serial`` traced on the cells, the boundary registers' cells as the ICL orders
    them; and each exported pattern's load, its ``iWrite`` of every register
    retargeted, the WSI bits it shifts. The PDL, if they all agree."""
    from faultflow.wrap.serial import INSTRUCTION

    interp, pdl, model = _interpreter(
        wrapper,
        scan_outs,
        mode="extest",
        top=top,
        scan_ins=scan_ins,
        scan_enable=scan_enable,
        instruction=INSTRUCTION,
    )
    play = _warptap("wsp_play")
    access = access_of(wrapper)
    theirs = tuple(
        SetupCycle(
            _levels(access, **{role: int(getattr(row.cycle, role)) for role in HELD}),
            (access.wrck,) if row.cycle.pulse else (),
        )
        for row in play.to_wsp_rows(interp.program)
    )
    if theirs != instruction_program(wrapper, INSTRUCTION):
        raise WspProgramError(
            "the WS_EXTEST setup program disagrees with warptap's PDL retarget of "
            "'iWrite WIR WS_EXTEST'"
        )
    registers = _wbr_registers(wrapper)
    (chain,) = serial["chains"]
    traced = [str(name) for name in chain["cells"]]
    ordered = [flop for _, flops in registers for flop in flops]
    if traced != ordered:
        first = next(
            (k for k, (a, b) in enumerate(zip(traced, ordered)) if a != b),
            min(len(traced), len(ordered)),
        )
        raise WspProgramError(
            "the boundary register traced from WSI to WSO isn't the ICL's: they "
            f"part at cell {first} ({len(traced)} cells against {len(ordered)})"
        )
    position = {flop: k for k, flop in enumerate(traced)}
    for number, pattern in enumerate(patterns):
        seq = [int(bit) for bit in pattern["load_seqs"]["0"]]
        if len(seq) != len(traced):
            raise WspProgramError(
                f"pattern {number} loads {len(seq)} bits into {len(traced)} cells"
            )
        # The cell k from WSI holds the bit shifted in len - 1 - k shifts before
        # the last; a register's value has its head as its MSB.
        for name, flops in registers:
            value = 0
            for flop in flops:
                value = (value << 1) | seq[len(seq) - 1 - position[flop]]
            interp.iWrite(name, value)
        rows = play.to_wsp_rows(interp.iApply())
        shifted = [int(row.cycle.wsi) for row in rows if row.cycle.shiftwr]
        if shifted != seq:
            raise WspProgramError(
                f"pattern {number}'s load disagrees with warptap's retarget of its "
                "boundary register writes"
            )
    return str(pdl.to_pdl_1500(interp.history, model))
