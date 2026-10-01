"""ff.py jtag on the JTAG-wrapped autoMBIST fixture, after its scan flow: the
network-integrity program is played through the TAP of the stitched netlist, and its
credit is reported beside the scan campaign's.

The TCK program is the fixture input_demo_8x16_scn4m_jtag_program.json -- warptap's
build_integrity_program for this network, exported with ff.py jtag --export -- so
these tests run without warptap; the one that builds the program itself needs it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.integrations.autombist import (
    load_autombist_manifest,
    synthesize_from_manifest,
)
from warptap_helpers import skip_unless_warptap

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/autombist"
JTAG_FIXTURE = FIXTURES / "input_demo_8x16_scn4m_jtag"
PROGRAM = FIXTURES / "input_demo_8x16_scn4m_jtag_program.json"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, Path]:
    """The wrapped design synthesized from its manifest, then init -> scan ->
    scan-check -> sim --scan: (work directory, top, .ofs)."""
    from faultflow.cli import main
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    work = tmp_path_factory.mktemp("jtag_flow")
    result = synthesize_from_manifest(
        load_autombist_manifest(JTAG_FIXTURE / "manifest.json"),
        out=work / "synth",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )
    top, ofs = result.top_module, result.ofs_path
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(work)
        for step in (["init"], ["scan"], ["scan-check"], ["sim", "--scan"]):
            assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    return work, top, ofs


def _run(
    scanned: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch, *args: str
) -> int:
    from faultflow.cli import main

    work, top, ofs = scanned
    monkeypatch.chdir(work)
    try:
        return main(["jtag", "--top", top, "-c", str(ofs), *args])
    except SystemExit as exc:  # parser.exit on a refusal
        return int(exc.code or 0)


def _report(scanned: tuple[Path, str, Path]) -> dict[str, Any]:
    work, top, ofs = scanned
    cfg = load_config(ofs, top)
    path = work / cfg.coverage_json_path
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _jtag_runs(scanned: tuple[Path, str, Path]) -> int:
    work, top, ofs = scanned
    cfg = load_config(ofs, top)
    with sqlite3.connect(work / cfg.db_path) as conn:
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'jtag_runs'"
        ).fetchone()[0]
        if not exists:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM jtag_runs").fetchone()[0])


def test_the_integrity_program_is_credited_beside_the_scan_campaign(
    scanned: tuple[Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _report(scanned)
    runs = _jtag_runs(scanned)

    assert _run(scanned, monkeypatch, "--program", str(PROGRAM)) == 0
    assert "graded=" in capsys.readouterr().out

    report = _report(scanned)
    summary, jtag, combined = report["summary"], report["jtag"], report["combined"]
    before["summary"].pop("fault_model", None)
    summary.pop("fault_model", None)
    assert summary == before["summary"]  # the scan campaign's own numbers stand
    assert jtag["graded"] == summary["structural_eligible"]
    assert jtag["detected"] > 0
    assert sum(jtag["detected_by_test"].values()) == jtag["detected"]
    assert jtag["detected_by_test"]["reset_instruction"] > 0
    assert jtag["scan_redundant_conflicts"] == []
    assert combined["detected"] >= summary["detected"]
    assert (
        combined["denominator"] + combined["redundant"]
        == summary["denominator"] + summary["redundant"]
    )
    for entry in report["autombist_categories"].values():
        assert entry["combined_detected"] >= entry["detected"]
    assert _jtag_runs(scanned) == runs + 1

    # The same program on the same netlist is up to date; --force grades again.
    assert _run(scanned, monkeypatch, "--program", str(PROGRAM)) == 0
    assert "up_to_date=true" in capsys.readouterr().out
    assert _jtag_runs(scanned) == runs + 1
    assert _run(scanned, monkeypatch, "--program", str(PROGRAM), "--force") == 0
    assert _jtag_runs(scanned) == runs + 2
    assert _report(scanned)["jtag"]["detected"] == jtag["detected"]


@pytest.mark.parametrize("test", ["reset_instruction", "instruction_register"])
def test_a_program_the_netlist_disagrees_with_is_refused_and_writes_nothing(
    test: str,
    scanned: tuple[Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """One care bit of the IDCODE readout (as if the IDCODE differed) or of the IR's
    capture (as if its width did) flipped: the golden gate names that test."""
    data = json.loads(PROGRAM.read_text(encoding="utf-8"))
    start = next(t["start"] for t in data["tests"] if t["name"] == test)
    period = data["care"].index("1", start)
    tdo = list(data["tdo"])
    tdo[period] = "1" if tdo[period] == "0" else "0"
    data["tdo"] = "".join(tdo)
    wrong = tmp_path / "wrong_program.json"
    wrong.write_text(json.dumps(data), encoding="utf-8")
    runs = _jtag_runs(scanned)

    assert _run(scanned, monkeypatch, "--program", str(wrong)) == 2
    assert f"in test {test} at TCK period {period}" in capsys.readouterr().err
    assert _jtag_runs(scanned) == runs


def test_reference_grading_agrees_with_bit_parallel(
    scanned: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scalar reference simulator, fault by fault, on an evenly spread sample."""
    from faultflow.db import connect
    from faultflow.db.campaign import latest_campaign_id
    from faultflow.jtag.grade import FaultRow, golden_gate, grade_faults
    from faultflow.jtag.program import load_program
    from faultflow.jtag.xcheck import JtagPorts, analyze
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map

    work, top, ofs = scanned
    monkeypatch.chdir(work)
    cfg = load_config(ofs, top)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    netlist = Path(manifest["generic_json"])
    cell_map = resolve_scan_cell_map(cfg)
    core = _load_core()
    common: dict[str, Any] = {
        "unsupported": cfg.simulation.unsupported_cells,
        "blackbox": list(cfg.blackbox_instances),
    }
    setup = analyze(
        core,
        netlist,
        cell_map,
        top,
        ports=JtagPorts(),
        holds={},
        unsupported=common["unsupported"],
        blackbox_instances=common["blackbox"],
    )
    program = load_program(PROGRAM)
    golden_gate(core, netlist, cell_map, program, setup, **common)
    with connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT id, fault_site_key, fault_type, compiled_net_index FROM faults "
            "WHERE campaign_id = ? AND exclusion = 'none' AND collapsed_into IS NULL "
            "ORDER BY id",
            (latest_campaign_id(conn, "scan"),),
        ).fetchall()
    sample = [FaultRow(int(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows][::29]
    assert len(sample) > 40
    args = (core, netlist, cell_map, program, setup, sample)
    fast = grade_faults(*args, sim_threads=2, **common)
    slow = grade_faults(*args, reference=True, **common)
    assert fast == slow
    assert fast.detected


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("iverilog", "vvp")),
    reason="needs Icarus Verilog on PATH",
)
def test_verify_replays_the_program_on_the_techmapped_netlist(
    scanned: tuple[Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Four-state, every flop starting X, the memory a floating stub: TDO is the
    golden value at every shift."""
    assert (
        _run(scanned, monkeypatch, "--program", str(PROGRAM), "--verify", "--force")
        == 0
    )
    assert "verified=true" in capsys.readouterr().out


def test_without_a_program_one_is_built_from_the_manifest(
    scanned: tuple[Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skip_unless_warptap("warptap.tap_integrity")
    exported = tmp_path / "program.json"
    assert _run(scanned, monkeypatch, "--force", "--export", str(exported)) == 0
    program = json.loads(exported.read_text(encoding="utf-8"))
    assert program["format"] == "warptap-tck-program"
    assert [t["name"] for t in program["tests"]][0] == "reset_instruction"
