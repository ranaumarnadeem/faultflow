"""A test setup program ([scan] test_setup): a chip whose test mode is a register a
program loads before the scan test, through ports of its own. The register stays out
of scan ([scan] nonscan_cells) and holds its value through every pattern, so scan
ties it there (a programmed non-scan flop). Every exported pattern starts with the
program; on the sky130 cells, applied alone or overlapped (the program given once,
first), the patterns unload as FaultFlow expects, their STIL's test_setup macro
expands to the same cycles, and the full scan protocol reproduces every credit.

The chip: mode, u_cfg__q, loads cfg_in when cfg_en is 1, else keeps its value. The
ring r0 -> r1 -> r2 reads d1 in place of d0 when mode is 1."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.scan.setup import SETUP_FORMAT
from scan_credit import credit_not_reproduced
from scan_replay import replay_on_cells

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "modechip"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
MUX = "sky130_fd_sc_hd__mux2_1"


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


def _gate(kind: str, a: int, b: int, x: int) -> dict[str, Any]:
    return _cell(f"sky130_fd_sc_hd__{kind}_1", A=a, B=b, X=x)


def _chip(cfg_clock: str) -> dict[str, Any]:
    """clk 2, d0 4, d1 5, cfg_clk 6, cfg_en 7, cfg_in 8; mode 10. The mode register
    on `cfg_clock` ("clk" or "cfg_clk"); r0 = mux(d0, d1, mode) ^ r2,
    r1 = r0 & d1, r2 = r1 | d0; y = r2 ^ r0."""
    clock = {"clk": 2, "cfg_clk": 6}[cfg_clock]
    cells = {
        "u_cfg__m": _cell(MUX, A0=10, A1=8, S=7, X=11),
        "u_cfg__q": _cell(DFXTP, CLK=clock, D=11, Q=10),
        "g_m": _cell(MUX, A0=4, A1=5, S=10, X=13),
        "g_x": _gate("xor2", 13, 19, 14),
        "r0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_a": _gate("and2", 15, 5, 16),
        "r1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 17, 4, 18),
        "r2": _cell(DFXTP, CLK=2, D=18, Q=19),
        "g_y": _gate("xor2", 19, 15, 20),
    }
    inputs = {"clk": 2, "d0": 4, "d1": 5, "cfg_clk": 6, "cfg_en": 7, "cfg_in": 8}
    ports = {n: {"direction": "input", "bits": [b]} for n, b in inputs.items()}
    ports["y"] = {"direction": "output", "bits": [20]}
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


# cfg_clk, a level, rises with cfg_en and cfg_in at 1; the test holds it at 0.
OWN_CLOCK = [
    {"inputs": {"cfg_en": 1, "cfg_in": 1}},
    {"inputs": {"cfg_en": 1, "cfg_in": 1, "cfg_clk": 1}},
]
# On the scan clock: one pulse with cfg_en and cfg_in at 1; the test holds cfg_en
# at 0, so the mux keeps the register's value while clk pulses on.
SCAN_CLOCK = [{"inputs": {"cfg_en": 1, "cfg_in": 1}, "pulse": ["clk"]}]


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _flow(work: Path, cfg_clock: str, program: list[Any], hold: str) -> Any:
    """init, scan, scan-check and sim --scan with the test setup program; the
    config and the exported patterns' path."""
    from faultflow.cli import main

    netlist = work / "chip.json"
    netlist.write_text(json.dumps(_chip(cfg_clock)), encoding="utf-8")
    setup = work / "setup.json"
    setup.write_text(
        json.dumps({"format": SETUP_FORMAT, "cycles": program}), encoding="utf-8"
    )
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n[fault_model]\ncollapsing = false\n\n"
        f"[scan]\nchains = 1\nnonscan_cells = u_cfg__*\nhold = {hold}\n"
        f"test_setup = {setup}\n",
        encoding="utf-8",
    )
    patterns = work / "patterns.json"
    steps = [["init"], ["scan"], ["scan-check"]]
    steps.append(["sim", "--scan", "--export-patterns", str(patterns)])
    for step in steps:
        assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    return load_config(ofs, TOP), patterns


@pytest.mark.integration
@pytest.mark.parametrize(
    "cfg_clock, program, hold",
    [("cfg_clk", OWN_CLOCK, "cfg_clk:0"), ("clk", SCAN_CLOCK, "cfg_en:0")],
)
def test_patterns_with_a_setup_program_unload_on_real_cells(
    tmp_path: Path, flow_tools: None, cfg_clock: str, program: Any, hold: str
) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = _flow(tmp_path, cfg_clock, program, hold)
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported and all(p["setup"] == program for p in exported)
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
        assert credit_not_reproduced(cfg, patterns) == []
        # Without the program the mode register powers up unknown on the cells,
        # and the patterns no longer unload as expected.
        bare = tmp_path / "bare.json"
        bare.write_text(
            json.dumps(
                [{k: v for k, v in p.items() if k != "setup"} for p in exported]
            ),
            encoding="utf-8",
        )
        assert replay_on_cells(cfg, bare, tmp_path / "bare_replay") != []


@pytest.mark.integration
def test_the_mode_register_is_programmed(tmp_path: Path, flow_tools: None) -> None:
    from faultflow.runner import Runner

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = _flow(tmp_path, "clk", SCAN_CLOCK, "cfg_en:0")
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        nonscan = Runner(cfg)._nonscan_setup(
            manifest, Path(str(manifest["generic_json"]))
        )
    assert nonscan is not None
    assert [(f.instance, f.value, f.programmed) for f in nonscan.flops] == [
        ("u_cfg__q", 1, True)
    ]


@pytest.mark.unit
def test_a_malformed_setup_file_is_refused_when_the_config_loads(
    tmp_path: Path,
) -> None:
    setup = tmp_path / "setup.json"
    ofs = tmp_path / "chip.ofs"
    ofs.write_text(f"[scan]\ntest_setup = {setup}\n", encoding="utf-8")
    setup.write_text(json.dumps({"cycles": []}), encoding="utf-8")
    with pytest.raises(ConfigError, match="not a test setup file"):
        load_config(ofs, TOP)
    setup.write_text(
        json.dumps({"format": SETUP_FORMAT, "cycles": [{"inputs": {"a": 2}}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not 0 or 1"):
        load_config(ofs, TOP)
    setup.write_text(json.dumps({"format": SETUP_FORMAT, "cycles": []}))
    with pytest.raises(ConfigError, match="no cycles"):
        load_config(ofs, TOP)
