"""ff.py mbist-insert and the Tcl shell's mbist_insert."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from mbist_chip import chip_copy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]


def _argv(spec: Path, out: Path, *extra: str) -> list[str]:
    return [
        "mbist-insert",
        "--top",
        "chip_top",
        "--spec",
        str(spec),
        "--out",
        str(out),
        *extra,
    ]


def test_the_cli_inserts_and_says_what_it_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(chip_copy(tmp_path), tmp_path / "out")) == 0
    out = capsys.readouterr().out
    assert "inserted 3 memories into chip_top" in out
    assert (
        "core0_ram: u_core0.u_mem (input_demo_8x16_scn4m_mbist_shell); test ports "
        "core0_ram_test_mode, core0_ram_bist_start, core0_ram_bist_done, "
        "core0_ram_bist_fail" in out
    )
    assert "module copies: core__mbist_u_core0 (of core)" in out
    assert (tmp_path / "out/chip_top_mbist.v").is_file()
    assert (tmp_path / "out/insertion.json").is_file()


def test_a_refusal_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = chip_copy(tmp_path, lambda text: text.replace("wmask0: 1", "wmask0: 0", 1))
    with pytest.raises(SystemExit) as exc:
        main(_argv(spec, tmp_path / "out"))
    assert exc.value.code == 2
    assert "but the design ties it to 1" in capsys.readouterr().err


def test_no_module_is_named_after_a_cell_of_the_ofs_liberty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    liberty = tmp_path / "cells.lib"
    liberty.write_text('library (demo) {\n  cell ("core__mbist_u_core0") { }\n}\n')
    ofs = tmp_path / "chip.ofs"
    ofs.write_text(f"[design]\nliberty = {liberty}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(_argv(chip_copy(tmp_path), tmp_path / "out", "-c", str(ofs)))
    assert exc.value.code == 2
    assert "would be named core__mbist_u_core0" in capsys.readouterr().err


def test_the_tcl_command_inserts(tmp_path: Path) -> None:
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))
    message = bridge.call(
        "mbist_insert",
        "-top",
        "chip_top",
        "-spec",
        str(chip_copy(tmp_path)),
        "-out",
        str(tmp_path / "out"),
    )
    assert "inserted 3 memories into chip_top" in str(message)
