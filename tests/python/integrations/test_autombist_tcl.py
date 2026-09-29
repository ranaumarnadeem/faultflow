"""Tests for the `autombist_generate` Tcl shell command.

Mirrors `tests/python/scan/test_retarget_cmd.py`'s Tcl-level testing shape
(`TclBridge(session).call(...)`/`.eval(...)`); the CLI surface for the same
feature is covered separately in `tests/python/cli/test_cli_autombist_generate.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _fake_autombist_cmd(tmp_path: Path) -> str:
    """A stand-in for the real `autombist` binary: copies the already-captured
    fixture into whatever --out directory it's given, exactly as a real
    `autombist generate --emit-manifest` would have -- no live autoMBIST
    invocation needed to exercise the Tcl plumbing."""
    stub = tmp_path / "fake_autombist.py"
    stub.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--out') + 1])\n"
        f"src = Path({str(FIXTURE)!r})\n"
        "dst = out / 'input_demo_8x16_scn4m'\n"
        "shutil.copytree(src, dst)\n",
        encoding="utf-8",
    )
    return f"python3 {stub}"


@pytest.mark.integration
def test_tcl_autombist_generate_loads_composed_netlist_and_blackboxes_memory(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    config_path = tmp_path / "unused.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    session = ProjectSession(output_root=tmp_path / "output")
    bridge = TclBridge(session)

    out = bridge.eval(
        f"autombist_generate -config {config_path} -out {out_dir} "
        f"-autombist_cmd {{{_fake_autombist_cmd(tmp_path)}}} "
        f"-liberty {SKY130_LIBERTY} -cell_lib {SKY130_CELL_MAP}"
    )
    assert out is not None
    assert "input_demo_8x16_scn4m_mbist" in str(out)

    # load_json + add_blackbox were both applied to the session in the SAME
    # shell session (run_atpg would work afterward without further setup).
    assert session.top == "input_demo_8x16_scn4m_mbist"
    assert session.synthesized is True
    assert session.declared_blackbox == ["u_sram"]

    ofs_path = out_dir / "input_demo_8x16_scn4m_mbist.ofs"
    cfg = load_config(ofs_path, "input_demo_8x16_scn4m_mbist")
    assert list(cfg.blackbox_instances) == ["u_sram"]


def test_tcl_autombist_generate_missing_required_flag_errors(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output")
    bridge = TclBridge(session)
    with pytest.raises(ShellError) as exc:
        bridge.call(
            "autombist_generate",
            "-config",
            "cfg.yml",
            "-out",
            str(tmp_path / "out"),
            # -liberty and -cell_lib both deliberately omitted.
        )
    assert "MISSING_ARG" in str(exc.value) or "required" in str(exc.value)


def test_tcl_autombist_generate_unknown_option_errors(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output")
    bridge = TclBridge(session)
    with pytest.raises(ShellError) as exc:
        bridge.call("autombist_generate", "-bogus", "x")
    assert "INVALID_OPTION" in str(exc.value) or "unknown option" in str(exc.value)


JTAG_FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m_jtag"


@pytest.mark.integration
@pytest.mark.parametrize("tap_nonscan", [False, True])
def test_tcl_autombist_generate_test_access_declares_its_clocks(
    tmp_path: Path, require_cpp_core: None, tap_nonscan: bool
) -> None:
    """-test_access wraps the generated design and loads the wrapped one: its
    two clocks are declared in the session, and the result says how many scan
    chains insertion needs. -tap_nonscan keeps the TAP and its IJTAG network
    out of scan, held in reset: tck clocks no scan flop, and the session's
    scan config carries the policy."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    stub = tmp_path / "fake_autombist.py"
    stub.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'generate':\n"
        "    out = Path(args[args.index('--out') + 1])\n"
        f"    shutil.copytree({str(JTAG_FIXTURE)!r}, out / 'input_demo_8x16_scn4m')\n"
        "elif args[0] != 'wrap-test-access':\n"
        "    sys.exit('unexpected arguments: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "unused.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")

    session = ProjectSession(output_root=tmp_path / "output")
    bridge = TclBridge(session)
    out = bridge.eval(
        f"autombist_generate -config {config_path} -out {tmp_path / 'out'} "
        f"-autombist_cmd {{python3 {stub}}} -test_access "
        f"-liberty {SKY130_LIBERTY} -cell_lib {SKY130_CELL_MAP}"
        + (" -tap_nonscan" if tap_nonscan else "")
    )

    chains, clocks = (1, ["clk"]) if tap_nonscan else (2, ["clk", "tck"])
    assert f"add_scan -chains {chains}" in str(out)
    assert session.top == "input_demo_8x16_scn4m_mbist"
    assert session.declared_blackbox == ["u_sram"]
    assert [c.port for c in session.declared_clocks] == clocks
    assert bool(session.scan_nonscan_cells) == tap_nonscan
    assert session.scan_holds == ((("trst_n", 0), ("tck", 0)) if tap_nonscan else ())


def test_tcl_autombist_generate_tap_nonscan_needs_test_access(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output")
    with pytest.raises(ShellError, match="-tap_nonscan needs -test_access"):
        TclBridge(session).call(
            "autombist_generate",
            *("-config", "c.yml", "-out", "out", "-liberty", "x.lib"),
            *("-cell_lib", "x.json", "-tap_nonscan"),
        )
