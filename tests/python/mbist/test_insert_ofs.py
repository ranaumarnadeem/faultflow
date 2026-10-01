"""The .ofs mbist-insert writes for the chip it synthesized (faultflow.mbist.ofs):
the user's .ofs, refused where it contradicts the scan holds the chip needs, merged
with the inserted design's netlist, outputs, blackboxes, clocks and scan settings."""

from __future__ import annotations

import configparser
import fnmatch
import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.mbist.netlist import InsertError
from faultflow.mbist.ofs import (
    check_base,
    nonscan_globs,
    read_base,
    scan_holds,
    write_inserted_ofs,
)
from faultflow.mbist.spec import ResetSpec
from mbist_chip import LIBERTY, ROOT, chip_copy
from warptap_helpers import skip_unless_warptap

CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
RESET = ResetSpec("rst_n", active_low=True)


def _base(tmp_path: Path, text: str) -> configparser.ConfigParser:
    ofs = tmp_path / "chip.ofs"
    ofs.write_text(text, encoding="utf-8")
    return read_base(ofs)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[scan]\nhold = rst_n:0\n", "holds rst_n at 0 in scan; it must be 1"),
        ("[scan]\nscan_enable = rst_n\n", "as a scan port"),
        ("[clocks]\nports = clk, rst_n\n", "as a clock"),
    ],
    ids=["held_active", "a_scan_port", "a_clock"],
)
def test_an_ofs_contradicting_the_reset_hold_is_refused(
    tmp_path: Path, text: str, match: str
) -> None:
    with pytest.raises(InsertError, match=match):
        check_base(_base(tmp_path, text), RESET, tap_nonscan=False)


@pytest.mark.unit
def test_with_the_tap_non_scan_trst_n_must_be_held_at_0(tmp_path: Path) -> None:
    base = _base(tmp_path, "[scan]\nhold = rst_n:1, trst_n:1\n")
    check_base(base, RESET, tap_nonscan=False)
    with pytest.raises(InsertError, match="holds trst_n at 1 in scan; it must be 0"):
        check_base(base, RESET, tap_nonscan=True)
    assert scan_holds(RESET, tap_nonscan=True) == (
        ("rst_n", 1),
        ("trst_n", 0),
        ("tck", 0),
    )
    assert scan_holds(ResetSpec("rst", active_low=False), tap_nonscan=False) == (
        ("rst", 0),
    )


@pytest.mark.unit
def test_a_shell_in_a_generate_block_gets_a_literal_glob() -> None:
    (glob,) = nonscan_globs(["g_bank[0].u_mem"])
    flop = "g_bank[0].u_mem__u_rst_sync__$auto$ff.cc:337:slice$759"
    assert fnmatch.fnmatchcase(flop, glob)
    assert not fnmatch.fnmatchcase(flop.replace("[0]", "0"), glob)


@pytest.mark.unit
def test_the_users_ofs_is_kept_and_the_chips_settings_added(tmp_path: Path) -> None:
    """Every section stays; the netlist, output root and manifest are the chip's;
    the blackboxes lose the memories now inside shells and gain the chip's; the
    clocks keep their off states; scan keeps the user's settings, adding chains
    up to the clock domains, the non-scan cells and the holds."""
    netlist = tmp_path / "composed.json"
    netlist.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    base = _base(
        tmp_path,
        f"[design]\nnetlist = old.json\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        "[fault_model]\nmodel = stuck_at\n\n"
        "[blackbox]\ninstances = u_core0.u_mem, u_pll\noutput_value = x, u_pll:0\n\n"
        "[clocks]\nports = clk, clk2\noff = clk2:1\n\n"
        "[scan]\nchains = 1\nscan_in = si\nhold = mode:0\nnonscan_cells = u_dbg__*\n",
    )
    ofs = write_inserted_ofs(
        tmp_path / "out/chip_mbist.ofs",
        base,
        netlist=netlist,
        output_root=tmp_path / "out/output",
        blackboxes=["u_core0.u_mem__u_collar.u_sram", "u_core1.u_mem"],
        renamed=["u_core0.u_mem"],
        clock_ports=["clk", "clk2"],
        chains=2,
        nonscan_cells=["u_core0.u_mem__u_rst_sync__*"],
        holds=[("rst_n", 1)],
        manifest=manifest,
    )
    cfg = load_config(ofs, "chip")
    assert cfg.netlist == netlist.resolve()
    assert cfg.output_root == (tmp_path / "out/output").resolve()
    assert cfg.autombist_manifest == manifest.resolve()
    assert cfg.fault_model.model == "stuck_at"
    assert cfg.blackbox_instances == (
        "u_pll",
        "u_core0.u_mem__u_collar.u_sram",
        "u_core1.u_mem",
    )
    assert dict(cfg.blackbox_output_values)["u_pll"] == "0"
    assert [(c.port, c.off_state) for c in cfg.clocks] == [("clk", 0), ("clk2", 1)]
    assert (cfg.scan.chains, cfg.scan.scan_in) == (2, "si")
    assert cfg.scan.nonscan_cells == ("u_dbg__*", "u_core0.u_mem__u_rst_sync__*")
    assert cfg.scan.hold == (("mode", 0), ("rst_n", 1))


@pytest.fixture(scope="module")
def inserted(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The fixture chip inserted with jtag and -c, the TAP non-scan: (out, .ofs)."""
    from faultflow.mbist.insert import insert_command

    if shutil.which("yosys") is None:
        pytest.skip("needs Yosys on PATH")
    skip_unless_warptap()
    tmp = tmp_path_factory.mktemp("insert_ofs")
    spec = chip_copy(tmp, lambda t: t.replace("jtag: false", "jtag: {tck_max_mhz: 10}"))
    base = tmp / "chip.ofs"
    base.write_text(
        f"[design]\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        "[atpg]\nmax_rounds = 5\n",
        encoding="utf-8",
    )
    out = tmp / "out"
    message = insert_command(spec, "chip_top", out, base, tap_nonscan=True)
    assert "chip_top_mbist.ofs" in message
    return out, out / "chip_top_mbist.ofs"


@pytest.mark.integration
def test_the_inserted_chips_ofs_scans_it(
    inserted: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """init and scan run from the written .ofs: the synthesized netlist, the
    reset synchronizers and the network left out of scan, one chain on clk."""
    from faultflow.cli import main

    out, ofs = inserted
    cfg = load_config(ofs, "chip_top")
    assert cfg.netlist.is_file() and cfg.output_root == (out / "output").resolve()
    assert cfg.atpg.max_rounds == 5  # the user's settings stay
    assert len(cfg.blackbox_instances) == 5  # three in shells, two not
    assert [c.port for c in cfg.clocks] == ["clk"]
    assert dict(cfg.scan.hold) == {"rst_n": 1, "trst_n": 0, "tck": 0}
    shells = ("u_core0.u_mem", "g_bank[[]0].u_mem", "u_mem_top")
    assert {f"{s}__u_rst_sync__*" for s in shells} <= set(cfg.scan.nonscan_cells)

    monkeypatch.chdir(out)
    for step in (["init"], ["scan"]):
        assert main([*step, "--top", "chip_top", "-c", str(ofs)]) == 0, step
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    nonscan = {
        ff["instance"]
        for ff in manifest["ineligible_ffs"]
        if ff["reason"] == "nonscan_policy"
    }
    assert sum("__u_rst_sync__" in name for name in nonscan) == 6
    assert any(name.startswith("warptap_") for name in nonscan)
    assert all(
        name.startswith("warptap_") or "__u_rst_sync__" in name for name in nonscan
    )
