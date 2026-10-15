"""The ICL (IEEE 1687) of a block's IEEE 1500 wrapper, and the integrity program
warptap builds from it.

The wrapper record (faultflow.wrap.record, control "wsp") is turned into warptap's model
of a wrapper (:func:`wrapper_model`): the boundary cells in their wrapper chains, head
first, the WSP's ports as the netlist names them, each wrapper chain's scan ports, and
faultflow's own instruction table (:data:`faultflow.wrap.wsp_model.OPCODES`, which of
them are serial, the (INTEST, EXTEST) mode each puts the cells in). warptap writes that
as ICL (:func:`wsp_icl`); :func:`program_from_warptap` reads the ICL back -- the ICL
alone must suffice -- and builds the integrity program from the imported model, as a
:class:`~faultflow.wrap.wsp_program.WspProgram` read against the record.

warptap is optional: without it the functions here say so, and a program file
(``ff.py wsp --program``) does the same job.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Mapping

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES, SERIAL, WIR_BITS, mode_of
from faultflow.wrap.wsp_program import (
    ROLES,
    WspProgram,
    WspProgramError,
    _check_block,
    check_wsp,
    is_soc,
    roles_of,
    serial_cells,
    soc_blocks,
)

_NEEDS_WARPTAP = (
    "building the WSP's integrity program needs warptap with its IEEE 1500 modules "
    "importable (pip install -e <a warptap clone>); pass a program file instead"
)


def _warptap(module: str) -> Any:
    try:
        return importlib.import_module(f"warptap.{module}")
    except ImportError as exc:
        raise WspProgramError(_NEEDS_WARPTAP) from exc


def wrapper_model(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> Any:
    """warptap's ``Wrapper1500`` for the block ``wrapper`` records. ``scan_ins`` and
    ``scan_outs``: each wrapper chain's scan ports, by chain index; ``top``: the
    wrapper's (ICL module's) name; ``scan_enable``: the scan enable port."""
    check_wsp(wrapper)
    _check_block(wrapper)
    model = _warptap("wsp1500_model")
    cells = serial_cells(wrapper)
    chain_ids = sorted({int(c["chain"]) for c in cells})
    for chain in chain_ids:
        for kind, ports in (("scan-in", scan_ins), ("scan-out", scan_outs)):
            if chain not in ports:
                raise WrapError(f"wrapper chain {chain} has no {kind} port")
    chains = tuple(
        tuple(
            model.WbrCell(str(c["label"]), str(c["side"]))
            for c in cells
            if int(c["chain"]) == chain
        )
        for chain in chain_ids
    )
    named = wrapper["wsp"]["ports"]
    ports = model.WspPorts(
        **{role: str(named[role]["port"]) for role in ROLES},
        scan_enable=scan_enable,
        chain_in=tuple(scan_ins[chain] for chain in chain_ids),
        chain_out=tuple(scan_outs[chain] for chain in chain_ids),
    )
    return model.Wrapper1500(
        name=top,
        chains=chains,
        ir_bits=WIR_BITS,
        instructions=_instructions(model, wrapper),
        ports=ports,
    )


def _instructions(model: Any, wrapper: Mapping[str, Any]) -> Any:
    """The instruction table: the codes the netlist's decode was built with (the
    record's), not the ones faultflow would build it with today."""
    recorded = wrapper["wsp"].get("opcodes", OPCODES)
    unknown = sorted(set(recorded) - set(OPCODES))
    if unknown:
        raise WrapError(
            f"the wrapper records instruction {unknown[0]}, which faultflow "
            "doesn't know"
        )
    return tuple(
        model.Instruction(name, int(code), name in SERIAL, mode_of(name))
        for name, code in recorded.items()
    )


def soc_design(wrapper: Mapping[str, Any], *, top: str) -> Any:
    """warptap's ``WspChainDesign`` for the SoC ``wrapper`` records: its blocks'
    wrappers from WSI to WSO, each in its own serial order, and the SoC's pins -- the
    port that drives each WSP role and whether an inverter sits between."""
    check_wsp(wrapper)
    model = _warptap("wsp1500_model")
    instructions = _instructions(model, wrapper)
    wrappers = tuple(
        model.Wrapper1500(
            name=block,
            chains=tuple(
                tuple(model.WbrCell(label, side) for label, side in chain)
                for chain in chains
            ),
            ir_bits=WIR_BITS,
            instructions=instructions,
        )
        for _instance, block, chains in soc_blocks(wrapper)
    )
    roles = roles_of(wrapper)
    wsp = wrapper["wsp"]
    pins = {
        role: (roles[role], bool(wsp["pins"].get(role, {}).get("inverted", False)))
        for role in ROLES
    }
    return model.WspChainDesign(top=top, wrappers=wrappers, pins=pins)


def wsp_icl(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
) -> str:
    """The ICL of the block ``wrapper`` records (see :func:`wrapper_model`), or of the
    SoC's chain of blocks (see :func:`soc_design`; ``scan_outs`` and ``scan_ins`` are
    a block's)."""
    emit = _warptap("icl_emit_1500")
    if is_soc(wrapper):
        design = soc_design(wrapper, top=top)
        return str(
            emit.to_icl_1500_chain(design.wrappers, top=design.top, pins=design.pins)
        )
    return str(
        emit.to_icl_1500(
            wrapper_model(
                wrapper, scan_outs, top=top, scan_ins=scan_ins, scan_enable=scan_enable
            )
        )
    )


def program_from_warptap(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    *,
    top: str,
    scan_ins: Mapping[int, str],
    scan_enable: str,
    margin: int = 8,
    exhaustive_opcodes: bool = True,
    icl_path: Path | None = None,
) -> WspProgram:
    """warptap's integrity program for the block (or the SoC's chain of blocks)
    ``wrapper`` records, built from the ICL :func:`wsp_icl` writes and read against
    the record. ``margin``: sentinel
    bits past each register's length; ``exhaustive_opcodes``: every code of the WIR,
    not only the instructions' own; ``icl_path``: where to write the ICL."""
    text = wsp_icl(
        wrapper, scan_outs, top=top, scan_ins=scan_ins, scan_enable=scan_enable
    )
    if icl_path is not None:
        icl_path.write_text(text, encoding="utf-8")
    importer = _warptap("icl_import_1500")
    integrity = _warptap("wsp_integrity")
    try:
        design = importer.import_icl_1500(text)
    except importer.Icl1500ImportError as exc:
        raise WspProgramError(f"warptap can't read the wrapper's ICL: {exc}") from exc
    # A SoC's block ports are its own nets, not pins: the tester drives none and sees
    # WSO alone.
    chain = {"drive": (), "scan_outs": False} if is_soc(wrapper) else {}
    try:
        program = integrity.build_wsp_integrity_program(
            design, margin=margin, exhaustive_opcodes=exhaustive_opcodes, **chain
        )
    except integrity.WspIntegrityError as exc:
        raise WspProgramError(f"warptap can't build the program: {exc}") from exc
    return WspProgram.from_json(program.to_json(), wrapper, scan_outs)
