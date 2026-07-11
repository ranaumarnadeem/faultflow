"""Optional USE_POWER_PINS support for the iverilog verification gate.

Sky130 behavioral models (cells/sky130/*.v) are `ifdef USE_POWER_PINS`-gated: the
default (unset) branch has NO VPWR/VGND ports at all (implicit supply1/supply0
nets), which is what every model in this repo's cell library uses out of the box --
so the default testbench correctly wires nothing extra. CLAUDE.md documents a
requirement to "drive VPWR=1 and VGND=0 in generated testbenches and define
USE_POWER_PINS if the selected models require it" for the OTHER branch, which was
entirely unimplemented (no config knob, no testbench wiring, no -D flag). This adds
an opt-in `[simulation] verify_use_power_pins` config flag that drives both.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.atpg import VectorSet
from faultflow.config import load_config
from faultflow.verify import IverilogVerifier, render_testbench


def _config(path: Path, extra_sim: str = "") -> None:
    path.write_text(
        f"""
[design]
netlist = missing.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json
liberty = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
{extra_sim}

[atpg]
mode = comb
output = missing.test
compaction = none

[report]
threshold = 95.0
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_config_verify_use_power_pins_defaults_false(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path)

    cfg = load_config(cfg_path, "demo")

    assert cfg.simulation.verify_use_power_pins is False


def test_config_verify_use_power_pins_reads_true(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _config(cfg_path, "verify_use_power_pins = true")

    cfg = load_config(cfg_path, "demo")

    assert cfg.simulation.verify_use_power_pins is True


def test_render_testbench_omits_power_pins_by_default() -> None:
    vectors = VectorSet(source="demo.test", input_order=["a"], vectors=[{"a": True}])

    text = render_testbench("demo", ["a"], ["y"], vectors)

    assert "VPWR" not in text
    assert "VGND" not in text


def test_render_testbench_drives_power_pins_when_enabled() -> None:
    vectors = VectorSet(source="demo.test", input_order=["a"], vectors=[{"a": True}])

    text = render_testbench("demo", ["a"], ["y"], vectors, use_power_pins=True)

    assert "reg VPWR;" in text
    assert "reg VGND;" in text
    assert ".VPWR(VPWR)" in text
    assert ".VGND(VGND)" in text
    assert "VPWR = 1'b1;" in text
    assert "VGND = 1'b0;" in text


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="iverilog/vvp not on PATH",
)
def test_iverilog_verifier_compiles_power_pins_gated_model(tmp_path: Path) -> None:
    """A gate module mirroring the real Sky130 `ifdef USE_POWER_PINS` shape: with
    power pins DISABLED the DUT has no VPWR/VGND ports (matches every cell in
    cells/sky130/*.v today); with them ENABLED the DUT requires VPWR/VGND and
    functionally gates its output on them (VPWR==1 && VGND==0), mirroring the real
    Sky130 UDP behavior. Proves -DUSE_POWER_PINS + the testbench wiring are both
    correctly threaded end to end, not just present in isolated unit tests."""
    model = tmp_path / "models.v"
    model.write_text("", encoding="utf-8")
    gate = tmp_path / "demo.v"
    gate.write_text(
        """
`ifdef USE_POWER_PINS
module demo(input a, output y, input VPWR, input VGND);
  assign y = (VPWR == 1'b1 && VGND == 1'b0) ? a : 1'b0;
endmodule
`else
module demo(input a, output y);
  assign y = a;
endmodule
`endif
""".strip()
        + "\n",
        encoding="utf-8",
    )
    vectors = VectorSet("demo.test", ["a"], [{"a": True}])

    # Without power pins: the `else` branch compiles and matches unconditionally.
    plain = IverilogVerifier(
        top="demo",
        work_dir=tmp_path / "verify_plain",
        gate_verilog=gate,
        verilog_models=[model],
    )
    plain_result = plain.run(["a"], ["y"], vectors)
    assert plain_result.passed is True
    assert plain_result.expected_outputs == [{"y": True}]

    # With power pins: the `ifdef` branch requires VPWR/VGND to be driven for the
    # output to follow `a` -- if the testbench failed to wire/drive them, iverilog
    # would either fail to compile (unconnected required port in strict modes) or
    # the functional check would see y=0 instead of y=1.
    powered = IverilogVerifier(
        top="demo",
        work_dir=tmp_path / "verify_powered",
        gate_verilog=gate,
        verilog_models=[model],
        use_power_pins=True,
    )
    powered_result = powered.run(["a"], ["y"], vectors)
    assert powered_result.passed is True
    assert powered_result.expected_outputs == [{"y": True}]
