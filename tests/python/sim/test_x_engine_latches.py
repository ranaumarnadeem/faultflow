"""Stage 3 of the X engine: latches. The X engine models a library latch cell
(transparent while enabled, holding otherwise, powered up unknown), and its
synthesis puts a design's RTL latches on the library's latch cells -- dfflibmap maps
flops only. A two-valued run's script is the one it always was, and a latch stays a
cell it does not model."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import load_config
from faultflow.pdk.x_synth import latch_cells, latch_techmap, x_map_lines
from faultflow.runner import Runner

ROOT = Path(__file__).resolve().parents[3]
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

# l opens while g is 1; m (a reset latch) while g is 0, reset by rn.
RTL = """
module xl(input d, input g, input rn, input a, output q, output r, output y);
  reg l;
  reg m;
  always @* if (g) l = d;
  always @* if (!rn) m = 1'b0; else if (!g) m = d;
  assign q = l;
  assign r = m;
  assign y = a & d;
endmodule
"""


def _sky130_map() -> dict[str, Any]:
    return json.loads(CELL_LIB.read_text(encoding="utf-8"))


def test_the_x_engine_finds_the_library_latch_of_each_enable_level() -> None:
    cells = latch_cells(_sky130_map())
    assert set(cells) == {"$_DLATCH_P_", "$_DLATCH_N_"}
    p = cells["$_DLATCH_P_"]
    assert p.cell == "sky130_fd_sc_hd__dlxtp_1"
    assert dict(p.pins) == {"d": "D", "en": "GATE", "q": "Q"}
    assert not p.ties
    assert cells["$_DLATCH_N_"].cell == "sky130_fd_sc_hd__dlxtn_1"
    techmap = latch_techmap(cells)
    assert "module \\$_DLATCH_P_ (input E, input D, output Q);" in techmap
    assert (
        "sky130_fd_sc_hd__dlxtp_1 _TECHMAP_REPLACE_ (.GATE(E), .D(D), .Q(Q));"
        in techmap
    )
    assert x_map_lines(cells, Path("m.v")) == (
        "dfflegalize -cell $_DLATCH_N_ x -cell $_DLATCH_P_ x t:$_DLATCH*\n"
        "techmap -map m.v\n"
    )


def test_a_library_latch_with_a_reset_is_used_with_the_reset_held_off() -> None:
    only_reset = {
        k: v
        for k, v in _sky130_map().items()
        if not k.startswith(
            ("sky130_fd_sc_hd__dlx", "sky130_fd_sc_hd__lpflow_inputisolatch")
        )
    }
    cell = latch_cells(only_reset)["$_DLATCH_P_"]
    assert cell.cell == "sky130_fd_sc_hd__dlrtp_1"
    assert dict(cell.ties) == {"RESET_B": "1"}
    assert ".RESET_B(1'b1)" in latch_techmap({"$_DLATCH_P_": cell})


def _cfg(tmp_path: Path, engine: str) -> Path:
    rtl = tmp_path / "xl.v"
    rtl.write_text(RTL, encoding="utf-8")
    (tmp_path / "schemas").mkdir(exist_ok=True)
    shutil.copy(ROOT / "schemas/coverage.schema.json", tmp_path / "schemas")
    path = tmp_path / "config.ofs"
    path.write_text(
        f"""
[design]
netlist = {rtl}
cell_lib = {CELL_LIB}
liberty = {LIBERTY}

[fault_model]
collapsing = false

[simulation]
unsupported_cells = fail
engine = {engine}

[atpg]
random_vectors = 32
max_rounds = 10

[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


def test_a_two_valued_script_is_the_one_it_always_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    two = Runner(load_config(_cfg(tmp_path, "two_valued"), "xl"))
    script = two._rendered_yosys_script()
    assert "dfflegalize" not in script and "techmap" not in script
    assert "dfflibmap -liberty" in script
    x = Runner(load_config(_cfg(tmp_path, "x"), "xl"))
    assert "dfflegalize -cell $_DLATCH_N_ x -cell $_DLATCH_P_ x" in (
        x._rendered_yosys_script()
    )


def test_an_rtl_latch_is_synthesized_to_library_cells_and_graded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("Yosys is required")
    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")
    monkeypatch.chdir(tmp_path)
    runner = Runner(load_config(_cfg(tmp_path, "x"), "xl"))
    runner.init()
    runner.sim(clean=True)
    netlist = json.loads(
        (runner.cfg.intermediate_dir / "xl.json").read_text(encoding="utf-8")
    )
    types = {c["type"] for c in netlist["modules"]["xl"]["cells"].values()}
    assert "sky130_fd_sc_hd__dlxtp_1" in types
    assert not any(t.startswith("$") for t in types)
    report = json.loads(runner.cfg.coverage_json_path.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["detected"] > 0
    assert summary["denominator"] > 0

    # Two-valued, the latch is a cell the engine does not model.
    two_dir = tmp_path / "two"
    two_dir.mkdir()
    monkeypatch.chdir(two_dir)
    two = Runner(load_config(_cfg(two_dir, "two_valued"), "xl"))
    two.init()
    with pytest.raises(Exception, match="(?i)unsupported cell"):
        two.sim(clean=True)
