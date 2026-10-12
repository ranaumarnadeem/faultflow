"""The test setup program of an IEEE 1500 WSP mode against warptap's PDL
(faultflow.wrap.wsp_pdl): the program that loads a mode's instruction into the WIR
(faultflow.wrap.wsp_setup.mode_program) is checked against what warptap's PDL
interpreter retargets ``iReset; iWrite WIR <instruction>; iApply`` to on the wrapper
serial port -- two independent derivations of the same cycles from the ICL's
instruction table, so a WIR bit order, a reset or update cycle, or an instruction
code the two read differently is caught before a test runs on it."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES
from faultflow.wrap.wsp_program import WspProgramError
from faultflow.wrap.wsp_setup import MODE_INSTRUCTIONS, mode_program
from warptap_helpers import skip_unless_warptap

ROLES = ("wsi", "wso", "shiftwr", "capturewr", "updatewr", "selectwir", "wrck", "wrstn")


def _record(opcodes: dict[str, int] | None = None) -> dict[str, Any]:
    ports = {role: {"port": role, "net": k} for k, role in enumerate(ROLES, 2)}
    cells = [("output", "y", 2, 0), ("input", "b", 1, 1), ("input", "a", 1, 0)]
    return {
        "control": "wsp",
        "wsp": {"ports": ports, "opcodes": dict(opcodes or OPCODES)},
        "cells": [
            {"side": side, "label": label, "chain": chain, "position": position}
            for side, label, chain, position in cells
        ],
    }


SCAN_OUTS = {1: "wbr_so0", 2: "wbr_so1"}
FIT: dict[str, Any] = {
    "top": "blk",
    "scan_ins": {1: "wbr_si0", 2: "wbr_si1"},
    "scan_enable": "scan_en",
}


@pytest.fixture(autouse=True)
def _needs_warptap() -> None:
    skip_unless_warptap("warptap.pdl_1500")


@pytest.mark.unit
@pytest.mark.parametrize("mode", sorted(MODE_INSTRUCTIONS))
def test_the_setup_program_is_what_warptaps_pdl_retargets_to(mode: str) -> None:
    from faultflow.wrap.wsp_pdl import warptap_mode_program

    record = _record()
    assert warptap_mode_program(record, SCAN_OUTS, mode=mode, **FIT) == mode_program(
        record, mode
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "line"),
    [
        ("functional", "iWrite WIR WS_BYPASS;"),
        ("intest", "iWrite WIR WP_INTEST;"),
        ("extest", "iWrite WIR WP_EXTEST;"),
    ],
)
def test_the_pdl_of_a_mode_is_a_reset_a_wir_write_and_an_apply(
    mode: str, line: str
) -> None:
    from faultflow.wrap.wsp_pdl import setup_pdl

    assert setup_pdl(_record(), SCAN_OUTS, mode=mode, **FIT).splitlines() == [
        "iReset;",
        line,
        "iApply;",
    ]


@pytest.mark.unit
def test_the_check_returns_the_pdl_it_checked() -> None:
    from faultflow.wrap.wsp_pdl import check_setup_program, setup_pdl

    record = _record()
    assert check_setup_program(record, SCAN_OUTS, mode="intest", **FIT) == setup_pdl(
        record, SCAN_OUTS, mode="intest", **FIT
    )


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["intest", "extest"])
def test_a_wrapper_built_with_other_instruction_codes_is_caught(mode: str) -> None:
    """The record says the netlist's decode was built with WP_INTEST and WP_EXTEST
    swapped; the setup program still loads the codes faultflow knows."""
    from faultflow.wrap.wsp_pdl import check_setup_program

    swapped = {
        **OPCODES,
        "WP_INTEST": OPCODES["WP_EXTEST"],
        "WP_EXTEST": OPCODES["WP_INTEST"],
    }
    with pytest.raises(WspProgramError, match="disagrees with warptap's PDL"):
        check_setup_program(_record(swapped), SCAN_OUTS, mode=mode, **FIT)


@pytest.mark.unit
def test_the_functional_mode_survives_swapped_parallel_codes() -> None:
    """WS_BYPASS is code 0 either way: only the modes naming a swapped code differ."""
    from faultflow.wrap.wsp_pdl import check_setup_program

    swapped = {
        **OPCODES,
        "WP_INTEST": OPCODES["WP_EXTEST"],
        "WP_EXTEST": OPCODES["WP_INTEST"],
    }
    check_setup_program(_record(swapped), SCAN_OUTS, mode="functional", **FIT)


@pytest.mark.unit
def test_a_record_naming_an_instruction_faultflow_does_not_know_is_refused() -> None:
    from faultflow.wrap.wsp_icl import wrapper_model

    with pytest.raises(WrapError, match="WS_MYSTERY"):
        wrapper_model(_record({**OPCODES, "WS_MYSTERY": 5}), SCAN_OUTS, **FIT)


@pytest.mark.unit
def test_without_warptap_the_message_says_what_to_do(monkeypatch: Any) -> None:
    from faultflow.wrap.wsp_pdl import setup_pdl

    for module in ("warptap.pdl_1500", "warptap.wsp_play"):
        monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(WspProgramError, match="warptap"):
        setup_pdl(_record(), SCAN_OUTS, mode="intest", **FIT)


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


@pytest.mark.integration
def test_ff_py_wsp_checks_each_modes_setup_program_and_writes_the_pdl(
    tmp_path: Any, flow_tools: None
) -> None:
    from faultflow.wrap.wsp_grade import run_wsp
    from wrap_flow import TOP, run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        run_wsp(cfg)
        for mode, instruction in MODE_INSTRUCTIONS.items():
            written = cfg.intermediate_dir / f"{TOP}_wsp_setup_{mode}.pdl"
            assert written.read_text(encoding="utf-8").splitlines() == [
                "iReset;",
                f"iWrite WIR {instruction};",
                "iApply;",
            ]
