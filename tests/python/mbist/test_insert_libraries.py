"""mbist-insert on a design's own libraries: its .ofs's [design] pdk,
std_cell_library and extra_libraries, the cells it instantiates of them (Sky130 hvl's
level shifter and flop beside hd) kept through insertion and the frozen synthesis,
and the chip scanned and replayed on both libraries' cells."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from faultflow.config import load_config
from mbist_chip import chip_copy
from pdk.installed import pdk_root_with
from scan_replay import replay_on_cells

TOP = "chip_top"
HVL = "sky130_fd_sc_hvl"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]

# Instantiated in chip_top: a 1.8 V -> 3.3 V level shifter, an hvl flop on it, back
# through hvl's down-shifter, and an hd buffer.
_CELLS = """
    wire hv_d, hv_q, lv_q;
    sky130_fd_sc_hvl__lsbuflv2hv_1 u_ls (.A(wdata[0]), .X(hv_d));
    sky130_fd_sc_hvl__dfxtp_1 u_hvl_ff (.CLK(clk), .D(hv_d), .Q(hv_q));
    sky130_fd_sc_hvl__lsbufhv2lv_1 u_back (.A(hv_q), .X(lv_q));
    sky130_fd_sc_hd__buf_1 u_hd_buf (.A(lv_q), .X(hv_out));
endmodule
"""


def _add_cells(tmp: Path) -> None:
    top = tmp / "rtl/chip_top.v"
    text = top.read_text(encoding="utf-8")
    text = text.replace(
        "output reg  [7:0] rdata_top_q",
        "output reg  [7:0] rdata_top_q,\n    output wire hv_out",
    )
    head, sep, _ = text.rpartition("endmodule")
    assert sep
    top.write_text(head + _CELLS, encoding="utf-8")


def test_mbist_insert_synthesizes_on_the_ofs_libraries_and_keeps_their_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.cli import main
    from faultflow.mbist.insert import insert_command
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    root = pdk_root_with("sky130A", HVL)
    if root is None or pdk_root_with("sky130A", "sky130_fd_sc_hd") != root:
        pytest.skip("needs sky130A's hd and hvl in one PDK root")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    spec = chip_copy(tmp_path, rtl_edit=_add_cells)
    base = tmp_path / "chip.ofs"
    base.write_text(
        "[design]\npdk = sky130A\nstd_cell_library = sky130_fd_sc_hd\n"
        f"extra_libraries = {HVL}\npdk_root = {root}\n\n[scan]\nchains = 8\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    insert_command(spec, TOP, out, base)
    ofs = out / f"{TOP}_mbist.ofs"
    monkeypatch.chdir(out)
    patterns = out / "patterns.json"
    for step in (
        ["init"],
        ["scan"],
        ["scan-check"],
        ["sim", "--scan", "--export-patterns", str(patterns)],
    ):
        assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    cfg = load_config(ofs, TOP)
    assert cfg.extra_libraries == (HVL,)

    scanned = (cfg.output_dir / f"{TOP}_scan.v").read_text(encoding="utf-8")
    cells = dict(
        (name.lstrip("\\"), cell)
        for cell, name in re.findall(
            r"^\s+([A-Za-z_]\w*)\s+(\\\S+|\w+)\s*\(", scanned, re.MULTILINE
        )
    )
    assert cells["u_ls"] == "sky130_fd_sc_hvl__lsbuflv2hv_1"
    assert cells["u_back"] == "sky130_fd_sc_hvl__lsbufhv2lv_1"
    assert cells["u_hd_buf"] == "sky130_fd_sc_hd__buf_1"
    # The hvl flop scanned by hvl's own scan flop; every other cell is hd's.
    assert cells["u_hvl_ff"] == "sky130_fd_sc_hvl__sdfxtp_1"
    others = {
        cell
        for name, cell in cells.items()
        if name not in {"u_ls", "u_back", "u_hvl_ff"}
    }
    library_cells = {c for c in others if c.startswith("sky130_")}
    assert library_cells and all(
        c.startswith("sky130_fd_sc_hd__") for c in library_cells
    )
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")
    assert replay_on_cells(cfg, patterns, out / "replay") == []
