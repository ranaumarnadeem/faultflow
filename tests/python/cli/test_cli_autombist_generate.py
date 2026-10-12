"""CLI-level smoke test for `ff.py autombist-generate`, driven against the
real checked-in autoMBIST fixture (no live autoMBIST invocation -- that's
covered separately by `test_autombist_invoke.py`'s subprocess-mocked unit
tests; this exercises the CLI wiring + full synthesis pipeline together,
mirroring how thoroughly other subcommands' CLI tests are exercised at this
level).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.integration
def test_autombist_generate_cli_writes_a_usable_ofs(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    # No live autoMBIST invocation: fake `--autombist-cmd` with a stub script
    # that just copies the already-captured fixture's manifest + RTL into
    # the requested --out directory, exactly as a real `autombist generate
    # --emit-manifest` would have. This exercises invoke_autombist_generate's
    # real subprocess + glob contract (unlike test_autombist_invoke.py's
    # subprocess.run mock) without depending on the real autombist package
    # being installed in whatever environment runs this test.
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

    out_dir = tmp_path / "out"
    # The stub above ignores --config entirely (it just copies the fixture),
    # but _handle_autombist_generate checks the config file exists before
    # doing anything, so a real (if unread) file is still required here.
    config_path = tmp_path / "unused.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")
    rc = main(
        [
            "autombist-generate",
            "--config",
            str(config_path),
            "--out",
            str(out_dir),
            "--autombist-cmd",
            f"python3 {stub}",
            "--liberty",
            str(SKY130_LIBERTY),
            "--cell-lib",
            str(SKY130_CELL_MAP),
        ]
    )
    assert rc == 0

    ofs_path = out_dir / "input_demo_8x16_scn4m_mbist.ofs"
    assert ofs_path.exists()
    cfg = load_config(ofs_path, "input_demo_8x16_scn4m_mbist")
    assert list(cfg.blackbox_instances) == ["u_sram"]
    assert cfg.netlist.exists()


@pytest.mark.parametrize("algo", [None, "march-raw"])
def test_autombist_generate_passes_algo_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, algo: str | None
) -> None:
    from types import SimpleNamespace

    import faultflow.integrations.autombist as autombist

    seen: dict[str, object] = {}

    def run(config, out, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            ofs_path=out / "d.ofs",
            top_module="d",
            block_count=1,
            instance_counts={},
            scan_chains=None,
            clock_ports=(),
            nonscan_cells=(),
            scan_holds=(),
            scan_test_access=False,
        )

    monkeypatch.setattr(autombist, "run_autombist_generate", run)
    config_path = tmp_path / "cfg.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")
    argv = [
        "autombist-generate",
        "--config",
        str(config_path),
        "--out",
        str(tmp_path / "out"),
        "--liberty",
        str(SKY130_LIBERTY),
        "--cell-lib",
        str(SKY130_CELL_MAP),
    ]
    if algo is not None:
        argv += ["--algo", algo]

    assert main(argv) == 0
    assert seen["algo"] == algo


def test_autombist_generate_rejects_missing_config(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yml"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "autombist-generate",
                "--config",
                str(missing),
                "--out",
                str(tmp_path / "out"),
                "--liberty",
                str(SKY130_LIBERTY),
                "--cell-lib",
                str(SKY130_CELL_MAP),
            ]
        )
    assert exc.value.code == 2


JTAG_FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m_jtag"


def _fake_autombist_with_test_access(tmp_path: Path) -> Path:
    """A stand-in for `autombist`: `generate` copies the captured design --
    already wrapped, so its manifest carries a test_access block -- and
    `wrap-test-access` leaves a marker in the directory it was given."""
    stub = tmp_path / "fake_autombist.py"
    stub.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'generate':\n"
        "    out = Path(args[args.index('--out') + 1])\n"
        f"    shutil.copytree({str(JTAG_FIXTURE)!r}, out / 'input_demo_8x16_scn4m')\n"
        "elif args[0] == 'wrap-test-access' and '--emit-icl' in args:\n"
        "    module_dir = Path(args[args.index('--manifest') + 1])\n"
        "    (module_dir / 'wrapped.marker').write_text('', encoding='utf-8')\n"
        "else:\n"
        "    sys.exit('unexpected arguments: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    return stub


def _generate_with_test_access(tmp_path: Path, *flags: str) -> tuple[int, Path, Path]:
    """autombist-generate --test-access (and `flags`) against the JTAG fixture:
    the exit code, the output directory and the .ofs it wrote."""
    stub = _fake_autombist_with_test_access(tmp_path)
    config_path = tmp_path / "unused.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = main(
        [
            "autombist-generate",
            "--config",
            str(config_path),
            "--out",
            str(out_dir),
            "--autombist-cmd",
            f"python3 {stub}",
            "--liberty",
            str(SKY130_LIBERTY),
            "--cell-lib",
            str(SKY130_CELL_MAP),
            "--test-access",
            *flags,
        ]
    )
    return rc, out_dir, out_dir / "input_demo_8x16_scn4m_mbist.ofs"


@pytest.mark.integration
def test_autombist_generate_cli_test_access_runs_the_tap_non_scan_by_default(
    tmp_path: Path, require_cpp_core: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The TAP and the IJTAG network are test instruments, tested through TCK by
    ff.py jtag, not by scan ATPG: left out of scan, held in reset. tck clocks no
    scan flop, so one clock and one chain are left."""
    from faultflow.integrations.autombist import (
        load_autombist_manifest,
        access_nonscan_settings,
    )

    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    rc, out_dir, ofs = _generate_with_test_access(tmp_path)

    assert rc == 0
    assert (out_dir / "input_demo_8x16_scn4m" / "wrapped.marker").exists()
    cfg = load_config(ofs, "input_demo_8x16_scn4m_mbist")
    assert list(cfg.blackbox_instances) == ["u_sram"]
    assert [c.port for c in cfg.clocks] == ["clk"]
    assert cfg.scan.chains == 1
    assert not cfg.scan.scan_test_access
    globs, holds = access_nonscan_settings(
        load_autombist_manifest(out_dir / "input_demo_8x16_scn4m" / "manifest.json")
    )
    assert globs and all(glob.endswith("__*") for glob in globs)
    assert (cfg.scan.nonscan_cells, cfg.scan.hold) == (globs, holds)
    assert holds == (("trst_n", 0), ("tck", 0))
    captured = capsys.readouterr()
    assert "non-scan:" in captured.out
    assert "deprecated" not in captured.err


@pytest.mark.integration
def test_autombist_generate_cli_scan_tap_grades_the_tap_with_scan_and_says_so(
    tmp_path: Path, require_cpp_core: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """--scan-tap is the opt-in to the old behaviour: the TAP and the network are
    scan flops on a clock domain of their own (tck), two clocks, two chains,
    and the .ofs records [scan] scan_test_access for the checks that refuse
    instruments on a chain without it."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    rc, _, ofs = _generate_with_test_access(tmp_path, "--scan-tap")

    assert rc == 0
    cfg = load_config(ofs, "input_demo_8x16_scn4m_mbist")
    assert [c.port for c in cfg.clocks] == ["clk", "tck"]
    assert cfg.scan.chains == 2
    assert cfg.scan.scan_test_access
    assert cfg.scan.nonscan_cells == () and cfg.scan.hold == ()
    captured = capsys.readouterr()
    assert "clocks: clk, tck  (scan chains: 2" in captured.out
    assert "--scan-tap" in captured.err and "ff.py jtag" in captured.err


@pytest.mark.integration
def test_autombist_generate_cli_tap_nonscan_is_a_deprecated_no_op(
    tmp_path: Path, require_cpp_core: None, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    rc, _, ofs = _generate_with_test_access(tmp_path, "--tap-nonscan")

    assert rc == 0
    cfg = load_config(ofs, "input_demo_8x16_scn4m_mbist")
    assert (cfg.scan.chains, cfg.scan.scan_test_access) == (1, False)
    assert "--tap-nonscan is deprecated" in capsys.readouterr().err


def _refused(tmp_path: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    config_path = tmp_path / "unused.yml"
    config_path.write_text("memory_name: unused\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "autombist-generate",
                "--config",
                str(config_path),
                "--out",
                str(tmp_path / "out"),
                "--liberty",
                str(SKY130_LIBERTY),
                "--cell-lib",
                str(SKY130_CELL_MAP),
                *flags,
            ]
        )
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_autombist_generate_scan_tap_needs_test_access(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "--scan-tap needs --test-access" in _refused(tmp_path, capsys, "--scan-tap")


def test_autombist_generate_tap_nonscan_and_scan_tap_contradict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _refused(tmp_path, capsys, "--test-access", "--tap-nonscan", "--scan-tap")
    assert "--tap-nonscan and --scan-tap contradict" in error
