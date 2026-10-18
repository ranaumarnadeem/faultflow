"""The ICL of a block's IEEE 1500 wrapper (faultflow.wrap.wsp_icl), written from its
wrapper record by warptap, and the integrity program warptap builds from that ICL
alone: the record -> warptap model -> ICL -> imported model -> program chain.

The program comes from an independent model of the WSP (warptap's), so it is held to
faultflow's own (faultflow.wrap.wsp_model) before anything is graded: the two agree at
every care bit, which also pins the instruction codes and the WIR's bit order the ICL
states."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES, SERIAL, WIR_BITS, mode_of
from faultflow.wrap.wsp_program import (
    WspProgram,
    WspProgramError,
    conformance_failures,
    program_tests,
)
from warptap_helpers import skip_unless_warptap

ROLES = ("wsi", "wso", "shiftwr", "capturewr", "updatewr", "selectwir", "wrck", "wrstn")


def _record(cells: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    ports = {role: {"port": role, "net": k} for k, role in enumerate(ROLES, 2)}
    return {
        "control": "wsp",
        "wsp": {"ports": ports, "opcodes": dict(OPCODES)},
        "cells": [
            {"side": side, "label": label, "chain": chain, "position": position}
            for side, label, chain, position in cells
        ],
    }


# Wrapper chains 1 and 2 (the core's scan chain is 0): chain 1 shifts a, b;
# chain 2 is y.
RECORD = _record([("output", "y", 2, 0), ("input", "b", 1, 1), ("input", "a", 1, 0)])
SCAN_INS = {1: "wbr_si0", 2: "wbr_si1"}
SCAN_OUTS = {1: "wbr_so0", 2: "wbr_so1"}
FIT: dict[str, Any] = {
    "top": "blk",
    "scan_ins": SCAN_INS,
    "scan_enable": "scan_en",
}


@pytest.fixture(autouse=True)
def _needs_warptap() -> None:
    skip_unless_warptap("warptap.wsp_integrity")


@pytest.mark.unit
def test_the_record_becomes_a_warptap_wrapper_with_faultflows_instructions() -> None:
    from faultflow.wrap.wsp_icl import wrapper_model

    model = wrapper_model(RECORD, SCAN_OUTS, **FIT)
    assert model.name == "blk" and model.ir_bits == WIR_BITS
    assert [[(c.label, c.side) for c in chain] for chain in model.chains] == [
        [("a", "input"), ("b", "input")],
        [("y", "output")],
    ]
    assert {i.name: i.code for i in model.instructions} == OPCODES
    for instruction in model.instructions:
        assert instruction.serial == (instruction.name in SERIAL), instruction.name
        assert instruction.mode == mode_of(instruction.name), instruction.name
    ports = model.ports
    assert (ports.wsi, ports.wso, ports.wrck, ports.wrstn) == (
        "wsi",
        "wso",
        "wrck",
        "wrstn",
    )
    assert [ports.si(k) for k in range(2)] == ["wbr_si0", "wbr_si1"]
    assert [ports.so(k) for k in range(2)] == ["wbr_so0", "wbr_so1"]
    assert ports.scan_enable == "scan_en"


@pytest.mark.unit
def test_the_icl_reads_back_to_the_wrapper_it_was_written_from() -> None:
    from warptap.icl_import_1500 import (  # type: ignore[import-not-found]
        import_icl_1500,
    )

    from faultflow.wrap.wsp_icl import wrapper_model, wsp_icl

    text = wsp_icl(RECORD, SCAN_OUTS, **FIT)
    assert text.startswith("Module blk") or "Module blk {" in text
    assert import_icl_1500(text) == wrapper_model(RECORD, SCAN_OUTS, **FIT)


@pytest.mark.unit
def test_a_record_whose_mode_isnt_set_by_a_wsp_has_no_icl() -> None:
    from faultflow.wrap.wsp_icl import wsp_icl

    with pytest.raises(WrapError, match="block's or a SoC's"):
        wsp_icl({**RECORD, "control": "pins"}, SCAN_OUTS, **FIT)
    # A SoC record without its WSP chain is no SoC's either.
    with pytest.raises(WrapError, match="no WSP chain"):
        wsp_icl({**RECORD, "blocks": []}, SCAN_OUTS, **FIT)


@pytest.mark.unit
def test_a_chain_without_scan_ports_is_refused_with_its_index() -> None:
    from faultflow.wrap.wsp_icl import wsp_icl

    with pytest.raises(WrapError, match="chain 2"):
        wsp_icl(RECORD, {1: "wbr_so0"}, **FIT)
    with pytest.raises(WrapError, match="chain 2"):
        wsp_icl(RECORD, SCAN_OUTS, **{**FIT, "scan_ins": {1: "wbr_si0"}})


@pytest.mark.unit
def test_the_program_warptap_builds_from_the_icl_conforms_to_faultflows_model() -> None:
    """Two independently written models of the WSP -- warptap's and faultflow's -- agree
    at every care bit of the program one built and the other replays."""
    from faultflow.wrap.wsp_icl import program_from_warptap

    program = program_from_warptap(RECORD, SCAN_OUTS, **FIT)
    assert program.source == "file"
    assert conformance_failures(program, RECORD, SCAN_OUTS) == []
    tests = program_tests(program.steps)
    assert tests[:2] == ["reset", "wir_length"]
    assert tests[-2:] == ["held_update", "capture_update"]
    codes = [t for t in tests if t.startswith("code_")]
    assert codes == [f"code_{code:0{WIR_BITS}b}" for code in range(1 << WIR_BITS)]
    # It cares about plenty: WSO at the sentinel positions of every register.
    assert sum(step.wso is not None for step in program.steps) > 100


@pytest.mark.unit
def test_the_margin_and_the_opcode_sweep_reach_warptaps_builder() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    base = program_from_warptap(RECORD, SCAN_OUTS, **FIT)
    longer = program_from_warptap(RECORD, SCAN_OUTS, margin=12, **FIT)
    assert len(longer) > len(base)
    own = program_from_warptap(RECORD, SCAN_OUTS, exhaustive_opcodes=False, **FIT)
    assert len(own) < len(base)
    assert conformance_failures(longer, RECORD, SCAN_OUTS) == []
    assert conformance_failures(own, RECORD, SCAN_OUTS) == []


@pytest.mark.unit
def test_the_program_drives_and_observes_what_the_record_names() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    program = program_from_warptap(RECORD, SCAN_OUTS, **FIT)
    names = {name for step in program.steps for name, _ in step.expected}
    assert names == {"y", "wbr_so0", "wbr_so1"}
    assert {label for step in program.steps for label, _ in step.ports} == {"a", "b"}


@pytest.mark.unit
def test_without_warptap_the_message_says_what_to_do(monkeypatch: Any) -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    for module in ("warptap.icl_emit_1500", "warptap.wsp_integrity"):
        monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(WspProgramError, match="warptap.*program file"):
        program_from_warptap(RECORD, SCAN_OUTS, **FIT)


@pytest.mark.unit
def test_a_program_warptap_built_round_trips_through_a_file_unchanged() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    program = program_from_warptap(RECORD, SCAN_OUTS, **FIT)
    again = WspProgram.from_json(program.to_json(RECORD, SCAN_OUTS), RECORD, SCAN_OUTS)
    assert again.steps == program.steps


# --- ff.py wsp: the program comes from warptap by default ---------------------------


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


def _detected(db: Any) -> set[int]:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return {
            int(row[0])
            for row in conn.execute(
                "SELECT fault_id FROM wsp_detections "
                "WHERE run_id = (SELECT MAX(id) FROM wsp_runs)"
            )
        }
    finally:
        conn.close()


@pytest.mark.integration
def test_the_program_ff_py_wsp_played_exports_and_plays_back_as_the_same_grade(
    tmp_path: Any, flow_tools: None
) -> None:
    from faultflow.cli import main
    from wrap_flow import TOP, run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        ofs = str(tmp_path / "chip.ofs")
        path = tmp_path / "wsp_program.json"
        assert main(["wsp", "--top", TOP, "-c", ofs, "--export", str(path)]) == 0
        played = _detected(cfg.db_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["format"] == "warptap-wsp-program"
        # The same program from the file: the recorded grade is up to date.
        assert main(["wsp", "--top", TOP, "-c", ofs, "--program", str(path)]) == 0
        assert _detected(cfg.db_path) == played and played


@pytest.mark.integration
def test_without_warptap_ff_py_wsp_says_so_and_a_program_file_still_grades(
    tmp_path: Any, flow_tools: None, monkeypatch: Any
) -> None:
    from faultflow.wrap.wsp_grade import run_wsp
    from wrap_flow import run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        path = tmp_path / "wsp_program.json"
        run_wsp(cfg, export=path)
        for module in ("warptap.icl_emit_1500", "warptap.wsp_integrity"):
            monkeypatch.setitem(sys.modules, module, None)
        with pytest.raises(WspProgramError, match="program file"):
            run_wsp(cfg, force=True)
        assert run_wsp(cfg, program=path).up_to_date


@pytest.mark.unit
def test_the_shells_run_wsp_takes_the_program_and_the_export() -> None:
    from pathlib import Path

    from faultflow.shell.errors import ShellError
    from faultflow.shell.tcl_bridge import TclBridge

    class Session:
        options: dict[str, Any] = {}

        def run_wsp(self, **options: Any) -> str:
            self.options = options
            return "ok"

    session = Session()
    bridge = TclBridge(session)  # type: ignore[arg-type]
    bridge._run_wsp(
        ["-program", "p.json", "-export", "e.json", "-force", "-threads", "2"]
    )
    assert session.options == {
        "program": Path("p.json"),
        "export": Path("e.json"),
        "force": True,
        "sim_threads": 2,
    }
    with pytest.raises(ShellError, match="usage: run_wsp"):
        bridge._run_wsp(["-program"])
