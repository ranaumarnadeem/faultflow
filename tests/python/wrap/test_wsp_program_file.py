"""The WSP integrity program as a file (faultflow.wrap.wsp_program.WspProgram): a
``warptap-wsp-program`` JSON -- the cycles, the data input ports it drives, the points
it observes (WSO, the wrapped outputs, the wrapper chains' scan-outs) with an expected
value and a care bit for each -- read against the block's wrapper record into the steps
``ff.py wsp`` grades with.

warptap's program written as a file reads back to the same steps, so a program file
grades exactly as the program warptap builds would; and a file that doesn't fit the
wrapper, or isn't a program, is refused before anything is graded."""

from __future__ import annotations

import copy
import functools
import json
from typing import Any

import pytest

from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_program import (
    FORMAT,
    VERSION,
    WspProgram,
    WspProgramError,
    conformance_failures,
    program_tests,
)
from warptap_helpers import wsp_program

ROLES = ("wsi", "wso", "shiftwr", "capturewr", "updatewr", "selectwir", "wrck", "wrstn")


def _record(cells: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    ports = {role: {"port": role, "net": k} for k, role in enumerate(ROLES, 2)}
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
SCAN_OUTS = {0: "wbr_so0", 1: "wbr_so1"}


def _file() -> dict[str, Any]:
    return _program().to_json(RECORD, SCAN_OUTS)


@functools.lru_cache(maxsize=None)
def _program() -> WspProgram:
    """warptap's program for RECORD (built once: it is the same every time)."""
    return wsp_program(RECORD, SCAN_OUTS)


@pytest.mark.unit
def test_warptaps_program_is_a_program_of_its_tests() -> None:
    program = _program()
    assert program.tests == program_tests(program.steps)
    assert program.tests[0] == "reset" and len(program) == len(program.steps) > 0


@pytest.mark.unit
def test_a_program_written_as_a_file_reads_back_to_the_same_steps() -> None:
    program = _program()
    data = json.loads(json.dumps(program.to_json(RECORD, SCAN_OUTS)))
    assert data["format"] == FORMAT and data["version"] == VERSION
    again = WspProgram.from_json(data, RECORD, SCAN_OUTS)
    assert again.source == "file"
    assert again.steps == program.steps
    assert again.tests == program.tests


@pytest.mark.unit
def test_the_file_names_the_roles_the_drive_the_observed_points_and_the_tests() -> None:
    data = _file()
    assert data["roles"] == {role: role for role in ROLES}
    assert data["drive"] == ["a", "b"]  # the input cells' ports, in serial order
    assert [(p["name"], p["kind"]) for p in data["observe"]] == [
        ("wso", "wso"),
        ("y", "output"),
        ("wbr_so0", "scan_out"),
        ("wbr_so1", "scan_out"),
    ]
    assert [t["name"] for t in data["tests"]] == program_tests(_program().steps)
    count = len(data["wrstn"])
    for column in ("selectwir", "shiftwr", "capturewr", "updatewr", "wsi", "pulse"):
        assert len(data[column]) == count
    for name in ("wso", "y", "wbr_so0", "wbr_so1"):
        assert (
            len(data["expect"][name]["value"])
            == len(data["expect"][name]["care"])
            == count
        )


@pytest.mark.unit
def test_a_scan_out_is_found_by_the_chain_not_by_the_name_the_file_gives_it() -> None:
    """warptap names a wrapper chain's scan-out ``so<k>``; the block's port may be
    called anything -- the chain index says which."""
    data = _file()
    renamed = copy.deepcopy(data)
    for point in renamed["observe"]:
        if point["kind"] == "scan_out":
            old = point["name"]
            point["name"] = f"so{point['index']}"
            renamed["expect"][point["name"]] = renamed["expect"].pop(old)
    assert WspProgram.from_json(renamed, RECORD, SCAN_OUTS).steps == _program().steps


@pytest.mark.unit
def test_a_no_care_bit_reads_as_no_care_and_a_care_bit_as_its_value() -> None:
    data = _file()
    wso = data["expect"]["wso"]
    at = wso["care"].index("1")
    assert wso["value"][at] in "01"
    data["expect"]["wso"]["care"] = "0" * len(wso["care"])
    program = WspProgram.from_json(data, RECORD, SCAN_OUTS)
    assert all(step.wso is None for step in program.steps)


def _mutations() -> list[tuple[str, Any, str]]:
    def drop(key: str):
        return lambda d: d.pop(key)

    def ragged(d: dict[str, Any]) -> None:
        d["shiftwr"] = d["shiftwr"][:-1]

    def not_binary(d: dict[str, Any]) -> None:
        d["wsi"] = d["wsi"][:-1] + "2"

    def extra_port(d: dict[str, Any]) -> None:
        d["drive"].append("z")
        d["inputs"]["z"] = "0" * len(d["wrstn"])

    def missing_port(d: dict[str, Any]) -> None:
        d["drive"].remove("b")
        d["inputs"].pop("b")

    def unknown_output(d: dict[str, Any]) -> None:
        d["observe"][1]["name"] = "nope"
        d["expect"]["nope"] = d["expect"].pop("y")

    def a_scan_out_the_block_lacks(d: dict[str, Any]) -> None:
        d["observe"][-1]["index"] = 7

    def an_input_cell_as_output(d: dict[str, Any]) -> None:
        d["observe"][1]["name"] = "a"
        d["expect"]["a"] = d["expect"].pop("y")

    def no_wso(d: dict[str, Any]) -> None:
        d["observe"] = [p for p in d["observe"] if p["kind"] != "wso"]

    def other_wrapper(d: dict[str, Any]) -> None:
        d["observe"][1]["wrapper"] = 1

    def roles(d: dict[str, Any]) -> None:
        d["roles"]["wsi"] = "tdi"

    def test_out_of_range(d: dict[str, Any]) -> None:
        d["tests"].append({"name": "x", "start": 3, "stop": 10**6})

    def empty(d: dict[str, Any]) -> None:
        for key in (
            "wrstn",
            "selectwir",
            "shiftwr",
            "capturewr",
            "updatewr",
            "wsi",
            "pulse",
        ):
            d[key] = ""

    return [
        (
            "format",
            lambda d: d.update(format="warptap-tap-program"),
            "warptap-wsp-program",
        ),
        ("version", lambda d: d.update(version=2), "version"),
        ("no-expect", drop("expect"), "malformed"),
        ("no-drive", drop("drive"), "malformed"),
        ("ragged", ragged, "column"),
        ("not-binary", not_binary, "column"),
        ("extra-port", extra_port, "drives"),
        ("missing-port", missing_port, "drive"),
        ("unknown-output", unknown_output, "nope"),
        ("scan-out-missing", a_scan_out_the_block_lacks, "scan-out"),
        ("input-cell-as-output", an_input_cell_as_output, "not an output"),
        ("no-wso", no_wso, "WSO"),
        ("other-wrapper", other_wrapper, "wrapper"),
        ("roles", roles, "wsi"),
        ("test-range", test_out_of_range, "range"),
        ("empty", empty, "no cycles"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutate", "message"),
    [(m, why) for _, m, why in _mutations()],
    ids=[name for name, _, _ in _mutations()],
)
def test_a_file_that_isnt_a_program_or_doesnt_fit_the_wrapper_is_refused(
    mutate: Any, message: str
) -> None:
    data = copy.deepcopy(_file())
    mutate(data)
    with pytest.raises(WspProgramError, match=message):
        WspProgram.from_json(data, RECORD, SCAN_OUTS)


@pytest.mark.unit
def test_the_refusals_are_wrap_errors_and_a_soc_or_pin_wrapper_has_no_program() -> None:
    assert issubclass(WspProgramError, WrapError)
    with pytest.raises(WrapError, match="block's or a SoC's"):
        WspProgram.from_json(_file(), {**RECORD, "control": "pins"}, SCAN_OUTS)
    # A record with blocks is a SoC's: without its WSP chain it can't be read.
    with pytest.raises(WrapError, match="no WSP chain"):
        WspProgram.from_json(_file(), {**RECORD, "blocks": []}, SCAN_OUTS)


@pytest.mark.unit
def test_a_program_that_isnt_json_text_is_refused_with_a_message() -> None:
    with pytest.raises(WspProgramError, match="JSON"):
        WspProgram.from_text("{not json", RECORD, SCAN_OUTS)
    with pytest.raises(WspProgramError, match="JSON object"):
        WspProgram.from_text("[1, 2]", RECORD, SCAN_OUTS)
    program = WspProgram.from_text(json.dumps(_file()), RECORD, SCAN_OUTS)
    assert program.steps == _program().steps


# --- the conformance gate: the program against faultflow's own model ----------------


def _flip(data: dict[str, Any], point: str, at: int) -> None:
    expect = data["expect"][point]
    expect["value"] = (
        expect["value"][:at]
        + ("1" if expect["value"][at] == "0" else "0")
        + expect["value"][at + 1 :]
    )


def _first_care(data: dict[str, Any], point: str) -> int:
    return data["expect"][point]["care"].index("1")


@pytest.mark.unit
def test_warptaps_program_conforms_to_the_model_and_so_does_its_file() -> None:
    program = _program()
    assert conformance_failures(program, RECORD, SCAN_OUTS) == []
    again = WspProgram.from_json(_file(), RECORD, SCAN_OUTS)
    assert conformance_failures(again, RECORD, SCAN_OUTS) == []


@pytest.mark.unit
@pytest.mark.parametrize("point", ["wso", "y", "wbr_so0", "wbr_so1"])
def test_a_care_bit_the_model_disagrees_with_is_a_failure_that_names_where(
    point: str,
) -> None:
    data = _file()
    at = _first_care(data, point)
    _flip(data, point, at)
    failures = conformance_failures(
        WspProgram.from_json(data, RECORD, SCAN_OUTS), RECORD, SCAN_OUTS
    )
    assert len(failures) == 1
    assert f"cycle {at}" in failures[0] and point in failures[0]
    assert "program" in failures[0] and "model" in failures[0]


@pytest.mark.unit
def test_a_care_bit_where_the_model_cant_know_is_a_failure() -> None:
    """The model leaves a register unknown until it is loaded: a program that cares
    about the first strobe, before any load, claims what nothing defines."""
    data = _file()
    n = len(data["wrstn"])
    unknown = next(
        k
        for k in range(n)
        if data["expect"]["y"]["care"][k] == "0" and data["selectwir"][k] == "0"
    )
    care = data["expect"]["y"]["care"]
    data["expect"]["y"]["care"] = care[:unknown] + "1" + care[unknown + 1 :]
    failures = conformance_failures(
        WspProgram.from_json(data, RECORD, SCAN_OUTS), RECORD, SCAN_OUTS
    )
    assert any(f"cycle {unknown}" in f and "unknown" in f for f in failures)


@pytest.mark.unit
def test_a_no_care_bit_never_fails_even_where_the_model_knows_the_value() -> None:
    data = _file()
    wso = data["expect"]["wso"]
    data["expect"]["wso"]["care"] = "0" * len(wso["care"])
    failures = conformance_failures(
        WspProgram.from_json(data, RECORD, SCAN_OUTS), RECORD, SCAN_OUTS
    )
    assert failures == []


@pytest.mark.unit
def test_a_program_whose_cycles_were_changed_no_longer_conforms() -> None:
    """The expectations were written for other cycles: a shift dropped from the WIR
    scan moves every later strobe."""
    data = _file()
    column = data["shiftwr"]
    at = column.index("1")
    data["shiftwr"] = column[:at] + "0" + column[at + 1 :]
    failures = conformance_failures(
        WspProgram.from_json(data, RECORD, SCAN_OUTS), RECORD, SCAN_OUTS
    )
    assert failures


@pytest.mark.unit
def test_a_point_the_block_doesnt_have_is_a_failure() -> None:
    import dataclasses

    steps = tuple(
        dataclasses.replace(step, expected=(*step.expected, ("nowhere", 1)))
        for step in _program().steps
    )
    failures = conformance_failures(WspProgram(steps, "file"), RECORD, SCAN_OUTS)
    assert failures and "nowhere" in failures[0]


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


def _block_program(manifest: dict[str, Any], scan_outs: dict[int, str]) -> Any:
    """warptap's program for the scanned block ``manifest`` records."""
    scan_ins = {
        int(c["index"]): str(c["scan_in"])
        for c in manifest["chains"]
        if c.get("kind") == "wrapper"
    }
    return wsp_program(
        manifest["wrapper"],
        scan_outs,
        scan_ins,
        top=str(manifest["top"]),
        scan_enable=str(manifest["scan_enable"]),
    )


def _detections(db: Any) -> list[tuple[int, str, int]]:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return [
            (int(f), str(t), int(c))
            for f, t, c in conn.execute(
                "SELECT fault_id, test, cycle FROM wsp_detections "
                "WHERE run_id = (SELECT MAX(id) FROM wsp_runs) ORDER BY fault_id"
            )
        ]
    finally:
        conn.close()


@pytest.mark.integration
def test_a_program_file_grades_exactly_as_the_program_warptap_builds(
    tmp_path: Any, flow_tools: None
) -> None:
    """warptap's program written as a file and handed to ff.py wsp is the same
    program: up to date against the run it came from, and -- forced -- the same
    faults detected by the same tests at the same cycles."""
    from faultflow.cli import main
    from faultflow.wrap.wsp_grade import run_wsp
    from wrap_flow import TOP, run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        assert main(["wsp", "--top", TOP, "-c", str(tmp_path / "chip.ofs")]) == 0
        built = _detections(cfg.db_path)
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        scan_outs = {
            int(c["index"]): str(c["scan_out"])
            for c in manifest["chains"]
            if c.get("kind") == "wrapper"
        }
        program = _block_program(manifest, scan_outs)
        path = tmp_path / "wsp_program.json"
        path.write_text(
            json.dumps(program.to_json(manifest["wrapper"], scan_outs)),
            encoding="utf-8",
        )
        assert run_wsp(cfg, program=path).up_to_date
        forced = run_wsp(cfg, force=True, program=path)
        assert not forced.up_to_date
        assert _detections(cfg.db_path) == built and built


@pytest.mark.integration
def test_a_program_the_model_disagrees_with_stops_at_the_conformance_gate(
    tmp_path: Any, flow_tools: None
) -> None:
    """A care bit flipped in the file is a program the model doesn't follow either:
    nothing is graded, and the message says where."""
    from faultflow.wrap.wsp_grade import WspGradeError, run_wsp
    from wrap_flow import run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        scan_outs = {
            int(c["index"]): str(c["scan_out"])
            for c in manifest["chains"]
            if c.get("kind") == "wrapper"
        }
        data = _block_program(manifest, scan_outs).to_json(
            manifest["wrapper"], scan_outs
        )
        wso = data["expect"]["wso"]
        at = wso["care"].index("1")
        flipped = "1" if wso["value"][at] == "0" else "0"
        wso["value"] = wso["value"][:at] + flipped + wso["value"][at + 1 :]
        path = tmp_path / "wsp_program.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(WspGradeError, match="disagrees with faultflow's WSP model"):
            run_wsp(cfg, force=True, program=path)
