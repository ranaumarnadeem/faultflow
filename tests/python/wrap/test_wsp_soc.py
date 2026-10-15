"""A SoC's chain of IEEE 1500 WSPs (faultflow.project.soc_wrapper): the blocks' WSPs
share the SoC's WRCK and controls -- some through inverters -- and chain WSI to WSO. Its
wrapper record gives warptap what it needs for the chain's ICL (each block's wrapper
and the SoC's pins) and the integrity program built from it; the program is held to
faultflow's own chain model, and the mode setup programs to warptap's PDL retarget of
``iWrite u<k>.WIR <instruction>`` on every block.

A SoC's block ports are its own nets, not pins, so the program drives none of them
(an input cell's capture is live, no care) and observes WSO alone."""

from __future__ import annotations

from typing import Any

import pytest

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES
from faultflow.wrap.wsp_program import (
    WspProgram,
    WspProgramError,
    conformance_failures,
    legacy_integrity_program,
    program_tests,
)
from faultflow.wrap.wsp_setup import MODE_INSTRUCTIONS, mode_program
from warptap_helpers import skip_unless_warptap


def _cell(
    instance: str, side: str, label: str, block_chain: int, block_position: int, k: int
) -> dict[str, Any]:
    return {
        "block": "blk",
        "instance": instance,
        "side": side,
        "label": f"blk/{label}",
        "ff": f"{instance}__ff{label}",
        "chain": 7,  # the SoC's scan chain, whatever the block's own was
        "position": k,
        "block_chain": block_chain,
        "block_position": block_position,
    }


def _soc() -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for n, instance in enumerate(("u_a", "u_b")):
        # Block chain 1 shifts a, b (head first); chain 2 is y.
        cells += [
            _cell(instance, "input", "a", 1, 0, 3 * n),
            _cell(instance, "input", "b", 1, 1, 3 * n + 1),
            _cell(instance, "output", "y", 2, 0, 3 * n + 2),
        ]
    # The cells come in the order the SoC record gives them, not the serial one.
    cells.reverse()

    def pin(port: str, inverted: bool = False) -> dict[str, Any]:
        return {"port": port, "inverted": inverted}

    return {
        "version": "faultflow_soc_wrapper_v1",
        "control": "wsp",
        "blocks": [
            {"block": "blk", "instance": "u_a"},
            {"block": "blk", "instance": "u_b"},
        ],
        "wsp": {
            "pins": {
                "wsi": pin("wsi"),
                "wrstn": pin("wrst", True),
                "selectwir": pin("selectwir"),
                "shiftwr": pin("shiftwr"),
                "capturewr": pin("capturewr"),
                "updatewr": pin("updatewr"),
            },
            "wrck": "wrck",
            "wso": "wso",
            "chain": ["u_a", "u_b"],
        },
        "cells": cells,
    }


SOC = _soc()


@pytest.fixture(autouse=True)
def _needs_warptap() -> None:
    skip_unless_warptap("warptap.wsp_integrity")


@pytest.mark.unit
def test_the_socs_wrappers_are_the_blocks_each_in_its_own_serial_order() -> None:
    from faultflow.wrap.wsp_icl import soc_design

    design = soc_design(SOC, top="soc")
    assert design.top == "soc"
    assert [w.name for w in design.wrappers] == ["blk", "blk"]
    for wrapper in design.wrappers:
        assert [[(c.label, c.side) for c in chain] for chain in wrapper.chains] == [
            [("blk/a", "input"), ("blk/b", "input")],
            [("blk/y", "output")],
        ]
        assert {i.name: i.code for i in wrapper.instructions} == OPCODES
    assert design.pins["wrstn"] == ("wrst", True)
    assert design.pins["wsi"] == ("wsi", False)
    assert design.pins["wrck"] == ("wrck", False)
    assert design.pins["wso"] == ("wso", False)


@pytest.mark.unit
def test_the_chain_follows_the_records_order_not_the_cell_lists() -> None:
    from faultflow.wrap.wsp_icl import soc_design

    soc = {**SOC, "wsp": {**SOC["wsp"], "chain": ["u_b", "u_a"]}}
    other = soc_design(soc, top="soc")
    assert [w.name for w in other.wrappers] == ["blk", "blk"]
    with pytest.raises(WrapError, match="u_c"):
        soc_design({**SOC, "wsp": {**SOC["wsp"], "chain": ["u_a", "u_c"]}}, top="soc")


@pytest.mark.unit
def test_the_icl_reads_back_to_the_socs_chain_with_its_pins() -> None:
    from warptap.icl_import_1500 import (  # type: ignore[import-not-found]
        import_icl_1500,
    )

    from faultflow.wrap.wsp_icl import soc_design, wsp_icl

    text = wsp_icl(SOC, {}, top="soc", scan_ins={}, scan_enable="scan_en")
    back: Any = import_icl_1500(text)
    assert back.top == "soc"
    assert back.wrappers == soc_design(SOC, top="soc").wrappers
    assert back.pins["wrstn"] == ("wrst", True)


@pytest.mark.unit
def test_the_program_warptap_builds_for_the_chain_conforms_to_the_chain_model() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    program = program_from_warptap(
        SOC, {}, top="soc", scan_ins={}, scan_enable="scan_en"
    )
    assert program.source == "file"
    assert conformance_failures(program, SOC) == []
    tests = program_tests(program.steps)
    assert tests[:2] == ["reset", "wir_length"]
    assert {t for t in tests if t.startswith("u0_")} >= {"u0_code_000", "u0_code_001"}
    assert {t for t in tests if t.startswith("u1_")} >= {"u1_code_000", "u1_code_100"}
    # It drives no block port and observes WSO alone.
    assert all(step.ports == () and step.expected == () for step in program.steps)
    assert sum(step.wso is not None for step in program.steps) > 100


@pytest.mark.unit
def test_a_socs_program_round_trips_through_a_file() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    program = program_from_warptap(
        SOC, {}, top="soc", scan_ins={}, scan_enable="scan_en"
    )
    data = program.to_json(SOC)
    assert data["roles"]["wrstn"] == "wrst" and data["roles"]["wso"] == "wso"
    assert data["drive"] == [] and [p["kind"] for p in data["observe"]] == ["wso"]
    again = WspProgram.from_json(data, SOC)
    assert again.steps == program.steps


@pytest.mark.unit
def test_a_file_that_drives_ports_or_observes_what_a_soc_hasnt_is_refused() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    data = program_from_warptap(
        SOC, {}, top="soc", scan_ins={}, scan_enable="scan_en"
    ).to_json(SOC)
    driven = {**data, "drive": ["u0.a"], "inputs": {"u0.a": "0" * len(data["wrstn"])}}
    with pytest.raises(WspProgramError, match="drives"):
        WspProgram.from_json(driven, SOC)
    outputs = {
        **data,
        "observe": [
            *data["observe"],
            {"name": "u0.y", "kind": "output", "wrapper": 0, "index": 2},
        ],
        "expect": {**data["expect"], "u0.y": data["expect"]["wso"]},
    }
    with pytest.raises(WspProgramError, match="observes"):
        WspProgram.from_json(outputs, SOC)


@pytest.mark.unit
def test_a_care_bit_the_chain_model_disagrees_with_is_a_failure() -> None:
    from faultflow.wrap.wsp_icl import program_from_warptap

    data = program_from_warptap(
        SOC, {}, top="soc", scan_ins={}, scan_enable="scan_en"
    ).to_json(SOC)
    expect = data["expect"]["wso"]
    at = expect["care"].index("1")
    expect["value"] = (
        expect["value"][:at]
        + ("1" if expect["value"][at] == "0" else "0")
        + expect["value"][at + 1 :]
    )
    failures = conformance_failures(WspProgram.from_json(data, SOC), SOC)
    assert len(failures) == 1 and f"cycle {at}" in failures[0]


@pytest.mark.unit
def test_faultflows_own_builder_grades_a_blocks_wsp_not_a_socs() -> None:
    with pytest.raises(WrapError, match="block's"):
        legacy_integrity_program(SOC)


@pytest.mark.unit
@pytest.mark.parametrize("mode", sorted(MODE_INSTRUCTIONS))
def test_the_socs_setup_program_is_what_warptaps_pdl_retargets_to(mode: str) -> None:
    """Every WIR of the chain written in one scan, the far block's bits first, the
    chip inputs through the pins -- WRSTN through its inverter."""
    from faultflow.wrap.wsp_pdl import setup_pdl, warptap_mode_program

    fit: dict[str, Any] = {"top": "soc", "scan_ins": {}, "scan_enable": "scan_en"}
    assert warptap_mode_program(SOC, {}, mode=mode, **fit) == mode_program(SOC, mode)
    assert setup_pdl(SOC, {}, mode=mode, **fit).splitlines() == [
        "iReset;",
        f"iWrite u0.WIR {MODE_INSTRUCTIONS[mode]};",
        f"iWrite u1.WIR {MODE_INSTRUCTIONS[mode]};",
        "iApply;",
    ]


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


@pytest.mark.integration
def test_ff_py_wsp_grades_the_faults_a_socs_scan_test_leaves_to_its_wsp_chain(
    tmp_path: Any, flow_tools: None
) -> None:
    """The SoC of two WSP blocks (soc_v2_fixtures, GLUE_WSP_RTL): after its EXTEST,
    ff.py wsp plays the program warptap builds from the chain's ICL -- WRSTN through
    its inverter, both WIRs loaded in one scan -- on the SoC's scanned netlist. Every
    fault the EXTEST left to the WSP chain is graded once and most are detected at the
    SoC's WSO; graded again only with force."""
    import json
    import sqlite3

    from faultflow.config import load_config
    from faultflow.project.manifest import load_soc_project
    from faultflow.project.soc_flow import run_soc_project
    from faultflow.service.flow import FlowService
    from faultflow.wrap.wsp_grade import run_wsp
    from soc_v2_fixtures import write_soc_v2

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        project = load_soc_project(write_soc_v2(tmp_path, wsp=True))
        base = load_config(project.base_config, project.soc.top)
        service = FlowService()
        scopes = run_soc_project(
            project,
            base,
            run_atpg=service.run_atpg,
            check_scan=service.check_scan,
            out=tmp_path / "out",
        )
        soc = scopes.soc
        outcome = run_wsp(soc)
        again = run_wsp(soc)
        report = json.loads(soc.coverage_json_path.read_text("utf-8"))
        block = json.loads(
            (soc.intermediate_dir / "wsp_report.json").read_text("utf-8")
        )
        conn = sqlite3.connect(soc.db_path)
        detections = conn.execute("SELECT COUNT(*) FROM wsp_detections").fetchone()[0]
        conn.close()
        top = project.soc.top
        for mode in ("functional", "intest", "extest"):
            pdl = soc.intermediate_dir / f"{top}_wsp_setup_{mode}.pdl"
            assert pdl.read_text("utf-8").count("iWrite u") == 2
        assert (soc.intermediate_dir / f"{top}_wsp.icl").exists()
    assert outcome.graded == report["summary"]["excluded_wsp"] > 0
    assert outcome.detected >= 0.5 * outcome.graded
    assert detections == outcome.detected == block["detected"]
    assert again.up_to_date and again.detected == outcome.detected
    assert block["combined"]["detected"] >= block["detected"]


@pytest.mark.integration
def test_a_block_whose_mode_pins_set_its_wrapper_has_no_wsp_to_grade(
    tmp_path: Any, flow_tools: None
) -> None:
    from faultflow.wrap.wsp_grade import WspGradeError, run_wsp
    from wrap_flow import run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest")
        with pytest.raises(WspGradeError, match="no IEEE 1500 WSP"):
            run_wsp(cfg)
