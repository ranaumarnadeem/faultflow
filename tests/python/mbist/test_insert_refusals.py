"""What mbist-insert refuses: an insertion file that doesn't match the design,
and names it would have to reuse."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

import pytest

from faultflow.mbist.insert import mbist_insert
from faultflow.mbist.netlist import InsertError
from mbist_chip import chip_copy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]


def _edit_core(old: str, new: str) -> Callable[[Path], None]:
    def edit(tmp: Path) -> None:
        path = tmp / "rtl/core.v"
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new), encoding="utf-8")

    return edit


def _core0(text: str, old: str, new: str) -> str:
    """Edit the core0_ram entry of the insertion file."""
    head, rest = text.split("  - name: core0_ram", 1)
    entry, tail = rest.split("  - name: bank0_ram", 1)
    assert old in entry
    return (
        head
        + "  - name: core0_ram"
        + entry.replace(old, new)
        + "  - name: bank0_ram"
        + tail
    )


CASES = [
    (
        "a tie that differs from the design's constant",
        lambda t: _core0(t, "wmask0: 1", "wmask0: 0"),
        None,
        "tie wmask0=0, but the design ties it to 1",
    ),
    (
        "a tie on a pin logic drives",
        None,
        _edit_core(".wmask0(1'b1)", ".wmask0(we)"),
        "tie wmask0=1, but the design drives it with net we",
    ),
    (
        "a pin outside the roles left undeclared",
        lambda t: _core0(t, "csb1: 1, ", ""),
        None,
        r"\['csb1'\] have no collar role and aren't declared",
    ),
    (
        "a role pin declared as a tie",
        lambda t: _core0(t, "csb1: 1,", "csb1: 1, csb0: 0,"),
        None,
        r"\['csb0'\] play a collar role",
    ),
    (
        "a shared clock that isn't the clock",
        None,
        _edit_core(".clk1  (clk)", ".clk1  (en)"),
        "share_clock pin clk1 isn't on the net of the clock pin clk0",
    ),
    (
        "an unused output that is read",
        None,
        _edit_core(
            "else        rdata_q <= dout;", "else        rdata_q <= dout ^ dout_port1;"
        ),
        "unused output dout1 is read in the design",
    ),
    (
        "an instance path that isn't there",
        lambda t: _core0(t, "instance: u_core0.u_mem", "instance: u_core9.u_mem"),
        None,
        "no instance at 'u_core9.u_mem'",
    ),
    (
        "no chip reset",
        lambda t: t.replace("reset: {port: rst_n, active: low}\n", ""),
        None,
        "needs the chip reset",
    ),
    (
        "one config, two algorithms",
        lambda t: _core0(t, "algo: march-c", "algo: march-raw"),
        None,
        r"two collars would both be named input_demo_8x16_scn4m_mbist: memories "
        r"\['core0_ram'\] \(algo march-raw\)",
    ),
]


@pytest.mark.parametrize(
    ("spec_edit", "rtl_edit", "message"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_a_wrong_insertion_is_refused(
    tmp_path: Path, spec_edit: Callable[[str], str] | None, rtl_edit, message: str
) -> None:
    spec = chip_copy(tmp_path, spec_edit or (lambda text: text), rtl_edit)
    with pytest.raises(InsertError, match=message):
        mbist_insert(spec, "chip_top", out=tmp_path / "out")
    # Refused before autoMBIST ran, where the design alone shows the problem.
    if "two collars" not in message and "copy" not in message:
        assert not (tmp_path / "out/work/gen0").exists()


def test_a_copy_name_already_taken_is_refused(tmp_path: Path) -> None:
    def add_module(tmp: Path) -> None:
        with (tmp / "rtl/chip_top.v").open("a", encoding="utf-8") as f:
            f.write("\nmodule core__mbist_u_core0; endmodule\n")

    spec = chip_copy(tmp_path, rtl_edit=add_module)
    with pytest.raises(InsertError, match="would be named core__mbist_u_core0"):
        mbist_insert(spec, "chip_top", out=tmp_path / "out")


def test_a_generated_module_name_already_taken_is_refused(tmp_path: Path) -> None:
    def add_module(tmp: Path) -> None:
        with (tmp / "rtl/chip_top.v").open("a", encoding="utf-8") as f:
            f.write("\nmodule faultflow_mbist_rst_sync; endmodule\n")

    spec = chip_copy(tmp_path, rtl_edit=add_module)
    with pytest.raises(
        InsertError, match="already has a module named faultflow_mbist_rst_sync"
    ):
        mbist_insert(spec, "chip_top", out=tmp_path / "out")


def test_a_liberty_cell_name_is_never_taken(tmp_path: Path) -> None:
    spec = chip_copy(tmp_path)
    with pytest.raises(InsertError, match="core__mbist_u_core0"):
        mbist_insert(
            spec, "chip_top", out=tmp_path / "out", taken={"core__mbist_u_core0"}
        )


def test_the_written_design_reads_back_with_the_original_problems_only(
    tmp_path: Path,
) -> None:
    """A problem the design already had (a wire used but never driven, in the
    module that gets copied) is no reason to refuse."""
    spec = chip_copy(
        tmp_path,
        rtl_edit=_edit_core(
            "    wire [7:0] dout_port1;",
            "    wire [7:0] dout_port1;\n    wire floating;\n"
            "    wire unused_and = floating & en;",
        ),
    )
    result = mbist_insert(spec, "chip_top", out=tmp_path / "out")
    assert re.search(r"module core__mbist_u_core0", result.rtl.read_text("utf-8"))
