"""The JTAG-wrapped autoMBIST fixture built with its TAP and IJTAG network non-scan
(synthesize_from_manifest(tap_nonscan=True): [scan] nonscan_cells for the
JTAG-category instances, [scan] hold = trst_n:0, tck:0): scan ties them in reset and
leaves their faults to JTAG, and ff.py jtag grades them.

The TCK program is the fixture input_demo_8x16_scn4m_jtag_program.json, so these
tests run without warptap."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.integrations.autombist import (
    load_autombist_manifest,
    synthesize_from_manifest,
)
from faultflow.integrations.autombist_coverage import JTAG_CATEGORIES
from scan_credit import credit_not_reproduced

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
    """init -> scan -> scan-check -> sim --scan with the TAP non-scan."""
    from faultflow.cli import main
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    work = tmp_path_factory.mktemp("tap_nonscan")
    result = synthesize_from_manifest(
        load_autombist_manifest(JTAG_FIXTURE / "manifest.json"),
        out=work / "synth",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
        tap_nonscan=True,
    )
    top, ofs = result.top_module, result.ofs_path
    # tck clocks no scan flop: one clock domain, one chain.
    cfg = load_config(ofs, top)
    assert ([c.port for c in cfg.clocks], cfg.scan.chains) == (["clk"], 1)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(work)
        for step in (
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(work / "patterns.json")],
        ):
            assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    return work, top, ofs


def _report(scanned: tuple[Path, str, Path]) -> dict[str, Any]:
    work, top, ofs = scanned
    cfg = load_config(ofs, top)
    return dict(json.loads((work / cfg.coverage_json_path).read_text(encoding="utf-8")))


def test_scan_leaves_the_tap_and_the_network_to_jtag(
    scanned: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, top, ofs = scanned
    monkeypatch.chdir(work)
    report = _report(scanned)
    summary = report["summary"]
    assert summary["excluded_jtag"] > 0
    # MBIST logic a TDR held at its reset value gates: untestable by scan with
    # the TAP in reset, not redundant.
    assert summary["hold_unresolved"] > 0
    categories = report["autombist_categories"]
    for category in JTAG_CATEGORIES & set(categories):
        assert categories[category]["denominator"] == 0, category
    rpt = (work / load_config(ofs, top).coverage_report_path).read_text()
    assert "run non-scan ([scan] nonscan_cells)" in rpt
    manifest = json.loads(load_config(ofs, top).scan_manifest_path.read_text())
    assert {ff["reason"] for ff in manifest["ineligible_ffs"]} == {"nonscan_policy"}


def test_every_credited_fault_reproduces_in_the_full_protocol(
    scanned: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view ties the non-scan flops and the held inputs; the full protocol
    simulates the real netlist with trst_n and tck held. Nothing scan credits may
    depend on the difference."""
    work, top, ofs = scanned
    monkeypatch.chdir(work)
    assert credit_not_reproduced(load_config(ofs, top), work / "patterns.json") == []


def test_jtag_brings_the_faults_scan_left_to_it_back(
    scanned: tuple[Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from faultflow.cli import main

    work, top, ofs = scanned
    monkeypatch.chdir(work)
    assert main(["jtag", "--top", top, "-c", str(ofs), "--program", str(PROGRAM)]) == 0
    assert "graded=" in capsys.readouterr().out
    report = _report(scanned)
    summary, jtag, combined = report["summary"], report["jtag"], report["combined"]
    left = summary["excluded_jtag"]
    assert jtag["graded"] == summary["structural_eligible"] + left
    assert combined["structural_eligible"] == summary["structural_eligible"] + left
    assert (
        combined["denominator"] + combined["redundant"]
        == summary["denominator"] + summary["redundant"] + left
    )
    assert combined["detected"] > summary["detected"]
    # Only JTAG tests the TAP and the network now; most of each is its credit.
    for name in JTAG_CATEGORIES & set(report["autombist_categories"]):
        entry = report["autombist_categories"][name]
        assert entry["combined_denominator"] > 0
        assert entry["combined_detected"] > 0.5 * entry["combined_denominator"], name
