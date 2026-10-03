"""A scan flop's clear and preset during shift (scan.shift_controls): a real scan
flop's clear and preset act whatever scan enable is (sky130's sdfrtp), so one that
toggles or sits active while the chains shift wipes the load or the unload --
FaultFlow's own simulators turn them off during shift and can't see it. scan-check
refuses a scan flop whose clear or preset isn't held inactive during shift; an input
that reaches one through buffers and inverters is held inactive for the whole test,
like a direct one.

The chips are stitched netlists of sky130 cells and FaultFlow's generic scan cells:
clk 2, rst_n 3, d0 4, d1 5, scan_en 6, scan_in 7, test_mode 8."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.scan.detection_pipeline import _scan_reset_pi_holds
from faultflow.scan.nonscan import analyze_nonscan
from faultflow.scan.shift_controls import ShiftViolation, shift_control_violations
from scan_replay import replay_on_cells

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
CELL_MAP = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
TOP = "chip"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"
SCAN_R = "$scanff_r_faultflow"
INPUTS = {"clk": 2, "rst_n": 3, "d0": 4, "d1": 5, "scan_en": 6, "scan_in": 7}


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    outputs = ("Q", "X", "Y")
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _scan_flop(d: int | str, sdi: int, reset_b: int | str, q: int) -> dict[str, Any]:
    return _cell(SCAN_R, CLK=2, D=d, SDI=sdi, SE=6, RESET_B=reset_b, Q=q)


def _module(cells: dict[str, Any], inputs: dict[str, int] | None = None) -> Any:
    ports = {
        **{
            n: {"direction": "input", "bits": [b]}
            for n, b in (inputs or INPUTS).items()
        },
        "scan_out": {"direction": "output", "bits": [12]},
    }
    return {"attributes": {"top": "1"}, "ports": ports, "cells": cells, "netnames": {}}


def _sync_chip(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A two-flop reset synchronizer s0 (D tied 1, non-scan) -> s1 on clk, cleared by
    rst_n; s1 resets r0, a scan flop (D = d0 ^ d1). s1 is scanned: it shifts r0's
    reset."""
    cells = {
        "s0": _cell(DFRTP, CLK=2, D="1", RESET_B=3, Q=10),
        "s1": _scan_flop(10, 7, 3, 11),
        "g_x": _cell("sky130_fd_sc_hd__xor2_1", A=4, B=5, X=14),
        "r0": _scan_flop(14, 11, 11, 12),
    }
    cells.update(overrides)
    return _module(cells)


def _manifest(nonscan: tuple[str, ...] = ("s0",)) -> dict[str, Any]:
    return {
        "clock_nets": [2],
        "scan_inputs": ["scan_in"],
        "scan_enable": "scan_en",
        "ineligible_ffs": [
            {"instance": name, "cell_type": DFRTP, "reason": "nonscan_policy"}
            for name in nonscan
        ],
    }


def _violations(
    module: dict[str, Any],
    holds: dict[str, int],
    nonscan: tuple[str, ...] = ("s0",),
) -> list[ShiftViolation]:
    manifest = _manifest(nonscan)
    setup = analyze_nonscan(
        module,
        manifest,
        CELL_MAP,
        globs=nonscan,
        holds=list(holds.items()),
        scan_reset_holds={},
    )
    return shift_control_violations(
        module, manifest, CELL_MAP, holds=holds, nonscan=setup
    )


@pytest.mark.unit
def test_a_reset_a_scan_flop_shifts_is_refused() -> None:
    (violation,) = _violations(_sync_chip(), {"rst_n": 1})
    assert (violation.instance, violation.pin) == ("r0", "RESET_B")
    assert "the output of scan flop s1" in violation.reason


@pytest.mark.unit
def test_from_a_settled_synchronizer_it_is_held() -> None:
    chip = _sync_chip(s1=_cell(DFRTP, CLK=2, D=10, RESET_B=3, Q=11))
    assert _violations(chip, {"rst_n": 1}, nonscan=("s0", "s1")) == []


@pytest.mark.unit
def test_gated_by_scan_enable_it_is_held() -> None:
    """r0's reset is s1's output OR scan enable: inactive whenever the chains shift."""
    chip = _sync_chip(
        g_r=_cell("sky130_fd_sc_hd__or2_1", A=11, B=6, X=15),
        r0=_scan_flop(14, 11, 15, 12),
    )
    assert _violations(chip, {"rst_n": 1}) == []


@pytest.mark.unit
def test_a_test_mode_bypass_mux_holds_it_with_test_mode_held() -> None:
    """The usual RTL fix: test_mode ? rst_n : s1 -- held only with test_mode held."""
    chip = _sync_chip(
        g_m=_cell("sky130_fd_sc_hd__mux2_1", A0=11, A1=3, S=8, X=15),
        r0=_scan_flop(14, 11, 15, 12),
    )
    chip["ports"]["test_mode"] = {"direction": "input", "bits": [8]}
    assert _violations(chip, {"rst_n": 1, "test_mode": 1}) == []
    (violation,) = _violations(chip, {"rst_n": 1})
    assert violation.instance == "r0"


@pytest.mark.unit
def test_an_input_the_patterns_assign_is_refused() -> None:
    """r0 reset by d1 & rst_n: d1 isn't held, so a pattern could set it 0."""
    chip = _sync_chip(
        s1=_cell(DFRTP, CLK=2, D=10, RESET_B=3, Q=11),
        g_r=_cell("sky130_fd_sc_hd__and2_1", A=5, B=3, X=15),
        r0=_scan_flop(14, 11, 15, 12),
    )
    (violation,) = _violations(chip, {"rst_n": 1}, nonscan=("s0", "s1"))
    assert violation.instance == "r0" and "the input d1" in violation.reason


@pytest.mark.unit
def test_a_hold_that_resets_a_scan_flop_through_logic_is_refused() -> None:
    """d1 held at 0 keeps r0 in reset through the AND: the chain can't shift."""
    chip = _sync_chip(
        s1=_cell(DFRTP, CLK=2, D=10, RESET_B=3, Q=11),
        g_r=_cell("sky130_fd_sc_hd__and2_1", A=5, B=3, X=15),
        r0=_scan_flop(14, 11, 15, 12),
    )
    (violation,) = _violations(chip, {"rst_n": 1, "d1": 0}, nonscan=("s0", "s1"))
    assert violation.instance == "r0" and "active level" in violation.reason


@pytest.mark.unit
def test_an_input_through_an_inverter_is_held_inactive(tmp_path: Path) -> None:
    """An active-high rst reaches r0's RESET_B through an inverter, r1's through two:
    held at 0 for the test, like rst_n wired straight to a pin is held at 1."""
    inputs = {"clk": 2, "rst": 3, "d0": 4, "d1": 5, "scan_en": 6, "scan_in": 7}
    cells = {
        "g_n": _cell("sky130_fd_sc_hd__inv_1", A=3, Y=9),
        "g_b": _cell("sky130_fd_sc_hd__buf_1", A=9, X=10),
        "r0": _scan_flop(4, 7, 9, 11),
        "r1": _scan_flop(5, 11, 10, 12),
    }
    netlist = tmp_path / "chip.json"
    netlist.write_text(
        json.dumps({"modules": {TOP: _module(cells, inputs)}}), encoding="utf-8"
    )
    assert _scan_reset_pi_holds(netlist, TOP, CELL_MAP) == {"rst": False}


@pytest.mark.unit
def test_an_input_two_pins_need_at_opposite_values_is_not_held(
    tmp_path: Path,
) -> None:
    """rst reaches r0 through an inverter and r1 straight: no value keeps both
    inactive, so neither is held (and scan-check refuses both flops)."""
    inputs = {"clk": 2, "rst": 3, "d0": 4, "d1": 5, "scan_en": 6, "scan_in": 7}
    cells = {
        "g_n": _cell("sky130_fd_sc_hd__inv_1", A=3, Y=9),
        "r0": _scan_flop(4, 7, 9, 11),
        "r1": _scan_flop(5, 11, 3, 12),
    }
    module = _module(cells, inputs)
    netlist = tmp_path / "chip.json"
    netlist.write_text(json.dumps({"modules": {TOP: module}}), encoding="utf-8")
    assert _scan_reset_pi_holds(netlist, TOP, CELL_MAP) == {}
    violations = shift_control_violations(
        module, _manifest(()), CELL_MAP, holds={}, nonscan=None
    )
    assert len(violations) == 2


# -- the flow: ff.py scan-check on the chip, then the patterns on real cells -------


def _flow_chip() -> dict[str, Any]:
    """Before scan: the synchronizer u_sync__s0 -> u_sync__s1 resets r0 and r1, a
    chain of two data flops; y = r1 & d1."""
    cells = {
        "u_sync__s0": _cell(DFRTP, CLK=2, D="1", RESET_B=3, Q=10),
        "u_sync__s1": _cell(DFRTP, CLK=2, D=10, RESET_B=3, Q=11),
        "g_x": _cell("sky130_fd_sc_hd__xor2_1", A=4, B=5, X=14),
        "r0": _cell(DFRTP, CLK=2, D=14, RESET_B=11, Q=15),
        "g_a": _cell("sky130_fd_sc_hd__and2_1", A=15, B=4, X=16),
        "r1": _cell(DFRTP, CLK=2, D=16, RESET_B=11, Q=17),
        "g_y": _cell("sky130_fd_sc_hd__and2_1", A=17, B=5, X=18),
    }
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in list(INPUTS.items())[:4]},
        "y": {"direction": "output", "bits": [18]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


def _run_flow(
    work: Path, nonscan: str, steps: list[list[str]], scan_extra: str = ""
) -> Any:
    """Each step's exit status (the CLI exits 2 on an error), and the config."""
    from faultflow.cli import main

    netlist = work / "chip.json"
    netlist.write_text(json.dumps(_flow_chip()), encoding="utf-8")
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n\n"
        f"[scan]\nchains = 1\nnonscan_cells = {nonscan}\nhold = rst_n:1\n{scan_extra}",
        encoding="utf-8",
    )
    results: list[int | str | None] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(work)
        for step in steps:
            try:
                results.append(main([*step, "--top", TOP, "-c", str(ofs)]))
            except SystemExit as exc:
                results.append(exc.code)
    return results, load_config(ofs, TOP)


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


@pytest.mark.integration
def test_scan_check_refuses_a_synchronizer_scan_would_shift(
    tmp_path: Path, flow_tools: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only s0 non-scan: s1 is a scan flop, and r0 and r1 reset from it."""
    results, _ = _run_flow(tmp_path, "u_sync__s0", [["init"], ["scan"], ["scan-check"]])
    assert results == [0, 0, 2]
    text = capsys.readouterr().err
    assert "held inactive during shift" in text
    assert "r0" in text and "r1" in text and "u_sync__s1" in text


@pytest.mark.integration
def test_settled_its_patterns_unload_on_real_cells_as_expected(
    tmp_path: Path, flow_tools: None
) -> None:
    """Both synchronizer flops settled: every unload of every exported pattern is
    the same on the sky130 cells, whose RESET_B acts during shift."""
    patterns = tmp_path / "patterns.json"
    results, cfg = _run_flow(
        tmp_path,
        "u_sync__*",
        [
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(patterns)],
        ],
    )
    assert results == [0, 0, 0, 0]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
def test_accepted_with_warn_the_cells_unload_otherwise(
    tmp_path: Path, flow_tools: None
) -> None:
    """[scan] shift_controls = warn lets s1 shift r0's and r1's reset: scan-check
    passes and sim --scan runs, but on the sky130 cells, whose RESET_B acts during
    shift, patterns unload otherwise than FaultFlow expects -- what scan-check
    refuses by default."""
    patterns = tmp_path / "patterns.json"
    results, cfg = _run_flow(
        tmp_path,
        "u_sync__s0",
        [
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(patterns)],
        ],
        scan_extra="shift_controls = warn\n",
    )
    assert results == [0, 0, 0, 0]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        (warning,) = [
            w for w in manifest["latest_check"]["warnings"] if "during shift" in w
        ]
        assert "u_sync__s1" in warning
        assert replay_on_cells(cfg, patterns, tmp_path / "replay")


@pytest.mark.unit
def test_shift_controls_is_fail_or_warn(tmp_path: Path) -> None:
    from faultflow.config import ConfigError

    ofs = tmp_path / "a.ofs"
    for text, want in (("", "fail"), ("shift_controls = warn\n", "warn")):
        ofs.write_text(
            f"[design]\ncell_lib = {CELL_MAP_PATH}\n\n[scan]\n{text}", encoding="utf-8"
        )
        assert load_config(ofs, TOP).scan.shift_controls == want
    ofs.write_text(
        f"[design]\ncell_lib = {CELL_MAP_PATH}\n\n[scan]\nshift_controls = maybe\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="shift_controls"):
        load_config(ofs, TOP)
