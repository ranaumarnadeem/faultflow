"""``ff.py wsp``: the IEEE 1500 WSP's own faults -- those every scan test leaves to it
(``excluded_wsp``) -- graded with its integrity program (faultflow.wrap.wsp_program)
compared at WSO (faultflow.wrap.wsp_grade).

The program exercises every instruction code, the WIR's shift path, WBY and the
boundary register's serial path, with the WSO the model expects. On a block
wrapped with control = wsp after its INTEST: the netlist passes the golden gate,
the grade credits most of the WSP's faults, and a WSP that doesn't behave as the
model says fails the gate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES, WIR_BITS
from faultflow.wrap.wsp_program import (
    integrity_program,
    program_tests,
    serial_cells,
)
from warptap_helpers import skip_unless_warptap


def _record(cells: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    """A block's wrapper record with these boundary cells (side, label, chain,
    position) and a WSP."""
    roles = ("wrck", "wrstn", "selectwir", "shiftwr", "capturewr", "updatewr")
    ports = {role: {"port": role, "net": k} for k, role in enumerate(roles, 2)}
    ports |= {"wsi": {"port": "wsi", "net": 20}, "wso": {"port": "wso", "net": 21}}
    return {
        "control": "wsp",
        "wsp": {"ports": ports},
        "cells": [
            {"side": side, "label": label, "chain": chain, "position": position}
            for side, label, chain, position in cells
        ],
    }


# Serial order: chain 0's cells (a, b), then chain 1's (y).
RECORD = _record([("output", "y", 1, 0), ("input", "b", 0, 1), ("input", "a", 0, 0)])


@pytest.mark.unit
def test_the_boundary_register_runs_chain_by_chain_each_from_its_head() -> None:
    assert [c["label"] for c in serial_cells(RECORD)] == ["a", "b", "y"]


@pytest.mark.unit
def test_the_integrity_program_exercises_the_wir_and_every_code() -> None:
    steps = integrity_program(RECORD)
    tests = program_tests(steps)
    codes = [f"code_{code:0{WIR_BITS}b}" for code in range(1 << WIR_BITS)]
    assert tests == ["reset", "wir_shift", *codes, "held_update"]
    # The WIR's shift stage: WSO is the bit that went in three shifts before.
    shifted = [s for s in steps if s.test == "wir_shift"]
    bits = [s.cycle.wsi for s in shifted]
    assert [s.wso for s in shifted[WIR_BITS:]] == bits[:-WIR_BITS]
    assert {0, 1} <= set(bits)
    for code in codes:
        cared = [s.wso for s in steps if s.test == code and s.wso is not None]
        assert len(cared) >= 3 and {0, 1} <= set(cared), code


@pytest.mark.unit
def test_ws_extest_shifts_the_captured_ports_out_through_every_cell() -> None:
    """After WS_EXTEST's capture the input cells hold their ports and the output
    cell its own value: the shift-out shows y, then b, then a."""
    steps = integrity_program(RECORD)
    test = f"code_{OPCODES['WS_EXTEST']:0{WIR_BITS}b}"
    part = [s for s in steps if s.test == test]
    capture = next(k for k, s in enumerate(part) if s.cycle.capturewr)
    captured = dict(part[capture].ports)
    out = [s.wso for s in part[capture + 1 :]]
    assert out[1:3] == [captured["b"], captured["a"]]
    assert out[0] is not None  # y: what the shift before left in it


@pytest.mark.unit
def test_a_soc_or_a_pin_controlled_wrapper_has_no_integrity_program() -> None:
    with pytest.raises(WrapError, match="block's"):
        integrity_program({**RECORD, "control": "pins"})
    with pytest.raises(WrapError, match="block's"):
        integrity_program({**RECORD, "blocks": []})


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


@pytest.mark.integration
def test_ff_py_wsp_grades_what_intest_left_to_the_wsp(
    tmp_path: Path, flow_tools: None
) -> None:
    """The ring's INTEST through the WIR, then ff.py wsp with faultflow's own
    integrity program ([wsp] builder = faultflow; the warptap-built one is graded in
    test_wsp_icl): every fault the INTEST left to the WSP graded, most detected, the
    report's wsp block and the side tables agreeing; graded again only with
    --force."""
    from faultflow.cli import main
    from wrap_flow import TOP, run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(
            tmp_path,
            "intest",
            sections="[wsp]\nbuilder = faultflow\n",
            wrap="control = wsp\n",
        )
        ofs = str(tmp_path / "chip.ofs")
        assert main(["wsp", "--top", TOP, "-c", ofs]) == 0
        block = json.loads((cfg.intermediate_dir / "wsp_report.json").read_text())
        report = json.loads(
            (cfg.intermediate_dir / "coverage_report.json").read_text("utf-8")
        )
        assert main(["wsp", "--top", TOP, "-c", ofs]) == 0  # up to date
        conn = sqlite3.connect(cfg.db_path)
        runs = conn.execute("SELECT COUNT(*) FROM wsp_runs").fetchone()[0]
        detections = conn.execute("SELECT COUNT(*) FROM wsp_detections").fetchone()[0]
        conn.close()
    assert block["graded"] == report["summary"]["excluded_wsp"] > 0
    assert block["detected"] >= 0.75 * block["graded"]
    assert runs == 1 and detections == block["detected"]
    assert sum(block["detected_by_test"].values()) == block["detected"]
    assert set(block["tests"]) >= {"reset", "wir_shift", "held_update"}
    combined = block["combined"]
    summary = report["summary"]
    # The WSP's faults join the scan's denominator, its credit the scan's.
    assert combined["detected"] == summary["detected"] + block["detected"]
    assert combined["denominator"] == summary["denominator"] + block["graded"]


@pytest.mark.integration
def test_a_wsp_that_isnt_its_model_fails_the_golden_gate(
    tmp_path: Path, flow_tools: None
) -> None:
    """WBY that never shifts (its mux's inputs swapped): the netlist's WSO isn't
    what the model expects, and nothing is graded."""
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.wrap.wsp_grade import (
        WspGradeError,
        benches,
        compared,
        golden_gate,
    )
    from wrap_flow import run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        data = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    module = data["modules"][manifest["top"]]
    wrapper = manifest["wrapper"]
    mux = module["cells"]["__wsp_wby_d"]["connections"]
    mux["A0"], mux["A1"] = mux["A1"], mux["A0"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data), encoding="utf-8")
    scan_outs = {
        int(c["index"]): str(c["scan_out"])
        for c in manifest["chains"]
        if c["kind"] == "wrapper"
    }
    steps = integrity_program(wrapper, scan_outs)
    ports = {role: str(p["port"]) for role, p in wrapper["wsp"]["ports"].items()}
    expected = compared(steps, ports["wso"])
    inputs = [
        name for name, port in module["ports"].items() if port["direction"] == "input"
    ]
    bench = benches(
        _load_core(),
        str(broken),
        str(resolve_scan_cell_map(cfg)),
        steps,
        inputs,
        ports,
        expected,
        unsupported="fail",
        blackbox=[],
    )
    with pytest.raises(WspGradeError, match="disagrees with the WSP's model"):
        golden_gate(bench, steps, expected)


@pytest.mark.integration
def test_the_shells_run_wsp_grades_after_intest(
    tmp_path: Path, flow_tools: None
) -> None:
    """The shell: the accumulator synthesized, wrapped with -control wsp, scanned,
    its INTEST, then run_wsp, the batch CLI's ff.py wsp -- with the program warptap
    builds from the wrapper's ICL."""
    skip_unless_warptap("warptap.wsp_integrity")
    from faultflow.cli import main
    from wrap_flow import ACCUMULATOR

    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    script = tmp_path / "flow.tcl"
    script.write_text(
        "\n".join(
            [
                f"read_netlist {{{rtl}}} -top acc",
                "use_lib_cells sky130",
                "add_clock clk -off 0",
                "synth",
                "wrap -control wsp",
                "add_scan -chains 1 -SI scan_in -SO scan_out -SE scan_en",
                "check_scan",
                "set_testmode intest",
                "run_atpg -scan -max 5",
                "run_wsp",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # From the repository: the shell finds the sky130 cells there.
    assert main(["shell", "-f", str(script), "--out", str(tmp_path / "output")]) == 0
    (db,) = list((tmp_path / "output").rglob("*.sqlite"))
    conn = sqlite3.connect(db)
    (graded,) = conn.execute("SELECT graded FROM wsp_runs").fetchone()
    (detected,) = conn.execute("SELECT COUNT(*) FROM wsp_detections").fetchone()
    conn.close()
    assert graded > 0 and detected > 0
