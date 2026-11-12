"""Stage 5 of the X engine: tristate buffers and buses. The X engine resolves a bus
from its drivers (one enabled: its value; a conflict or none: X), and its synthesis
keeps a design's ``1'bz`` drivers as the library's tristate buffers (``tribuf``) --
two-valued synthesis reads ``1'bz`` as a don't-care and merges them, as before."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import load_config
from faultflow.pdk.x_synth import latch_techmap, tristate_cell
from faultflow.runner import Runner

ROOT = Path(__file__).resolve().parents[3]
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TBUS = ROOT / "tests/cpp/fixtures/tiny_tbus.json"

RTL = """
module tb(input a, input b, input oe1, input oe2, input c, output bus, output y);
  wire w;
  assign w = oe1 ? a : 1'bz;
  assign w = oe2 ? b : 1'bz;
  assign bus = w;
  assign y = w & c;
endmodule
"""


def test_the_x_engine_maps_a_tristate_to_the_library_buffer() -> None:
    cell = tristate_cell(json.loads(CELL_LIB.read_text(encoding="utf-8")))
    assert cell is not None
    # Sky130 has no non-inverting buffer enabled high: ebufn, its enable inverted.
    assert cell.cell == "sky130_fd_sc_hd__ebufn_1"
    assert (cell.data, cell.enable, cell.output) == ("A", "TE_B", "Z")
    assert not cell.active_high and not cell.invert
    techmap = latch_techmap({}, cell)
    assert "module \\$_TBUF_ (input A, input E, output Y);" in techmap
    assert "wire e_n = ~E;" in techmap
    assert ".A(A), .TE_B(e_n), .Z(Y)" in techmap


def _runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    netlist: Path,
    top: str,
    engine: str,
) -> Runner:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schemas").mkdir(exist_ok=True)
    shutil.copy(ROOT / "schemas/coverage.schema.json", tmp_path / "schemas")
    path = tmp_path / "config.ofs"
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_LIB}
liberty = {LIBERTY}

[fault_model]
collapsing = false

[simulation]
engine = {engine}

[atpg]
random_vectors = 32
max_rounds = 5

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    runner = Runner(load_config(path, top))
    runner.init()
    return runner


@pytest.fixture(scope="module")
def core() -> Any:
    loaded = runner_mod._load_core()
    if loaded is None:
        pytest.skip("C++ extension _faultflow_core is required")
    return loaded


def test_a_tristate_bus_netlist_is_graded_on_the_x_engine_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    x = _runner(tmp_path / "x", monkeypatch, TBUS, "tiny_tbus", "x")
    x.sim(clean=True)
    report = json.loads(x.cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
    two = _runner(tmp_path / "two", monkeypatch, TBUS, "tiny_tbus", "two_valued")
    with pytest.raises(Exception, match="(?i)unsupported cell"):
        two.sim(clean=True)


def test_an_rtl_tristate_bus_is_synthesized_to_buffers_and_graded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: Any
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("Yosys is required")
    rtl = tmp_path / "tb.v"
    tmp_path.mkdir(parents=True, exist_ok=True)
    rtl.write_text(RTL, encoding="utf-8")
    runner = _runner(tmp_path / "run", monkeypatch, rtl, "tb", "x")
    assert "tribuf\nflatten" in runner._rendered_yosys_script()
    runner.sim(clean=True)
    netlist = json.loads(
        (runner.cfg.intermediate_dir / "tb.json").read_text(encoding="utf-8")
    )
    types = [c["type"] for c in netlist["modules"]["tb"]["cells"].values()]
    assert types.count("sky130_fd_sc_hd__ebufn_1") == 2
    assert not any(t.startswith("$") for t in types)
    report = json.loads(runner.cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
