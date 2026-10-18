"""autombist-generate on a PDK's libraries (--pdk, --std-cell-library,
--extra-libraries, --pdk-root): resolved as an .ofs's [design] is, synthesized on,
and named so in the .ofs it writes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.integrations.autombist import (
    generate_libraries,
    load_autombist_manifest,
    synthesize_from_manifest,
)
from pdk.installed import pdk_root_with

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
HD, HVL = "sky130_fd_sc_hd", "sky130_fd_sc_hvl"


def _root() -> Path:
    root = pdk_root_with("sky130A", HVL)
    if root is None or pdk_root_with("sky130A", HD) != root:
        pytest.skip("needs sky130A's hd and hvl in one PDK root")
    return root


@pytest.mark.unit
def test_autombist_generate_needs_a_liberty_and_cell_map_or_a_pdk_library() -> None:
    with pytest.raises(ConfigError, match="--std-cell-library"):
        generate_libraries(liberty=ROOT / "x.lib")


@pytest.mark.unit
def test_a_liberty_and_cell_map_are_used_as_given() -> None:
    liberty = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
    cell_lib = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
    libraries = generate_libraries(liberty=liberty, cell_lib=cell_lib)
    assert libraries.synth.liberty == liberty.resolve()
    assert libraries.synth.cell_files == (liberty.resolve(),)
    assert libraries.cell_lib == cell_lib.resolve()
    assert libraries.design == (
        ("liberty", str(liberty.resolve())),
        ("cell_lib", str(cell_lib.resolve())),
    )


@pytest.mark.integration
def test_the_cli_passes_the_pdk_libraries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.integrations.autombist as autombist
    from faultflow.cli import main

    root = _root()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    seen: dict[str, Any] = {}

    def run(config: Path, out: Path, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(autombist, "run_autombist_generate", run)
    (tmp_path / "c.yml").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(
            [
                "autombist-generate",
                "--config",
                str(tmp_path / "c.yml"),
                "--out",
                str(tmp_path / "out"),
                "--pdk",
                "sky130A",
                "--std-cell-library",
                HD,
                "--extra-libraries",
                HVL,
                "--pdk-root",
                str(root),
            ]
        )
    assert dict(seen["design"]) == {
        "pdk": "sky130A",
        "std_cell_library": HD,
        "extra_libraries": HVL,
        "pdk_root": str(root.resolve()),
    }
    assert [p.name for p in seen["liberty"].cell_files] == [
        "sky130_fd_sc_hd__tt_025C_1v80.lib",
        "sky130_fd_sc_hvl__tt_025C_3v30.lib",
        "sky130_fd_sc_hvl__tt_025C_3v30_lv1v80.lib",
    ]
    assert seen["liberty"].dont_use  # the cells hd's generated map doesn't model


@pytest.mark.integration
def test_a_design_synthesized_on_a_pdk_library_runs_its_scan_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    from faultflow.cli import main

    root = _root()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    libraries = generate_libraries(
        pdk="sky130A", std_cell_library=HD, extra_libraries=[HVL], pdk_root=root
    )
    result = synthesize_from_manifest(
        load_autombist_manifest(FIXTURE / "manifest.json"),
        out=tmp_path / "synth",
        liberty=libraries.synth,
        cell_lib=libraries.cell_lib,
        design=libraries.design,
    )
    text = result.ofs_path.read_text(encoding="utf-8")
    assert f"std_cell_library = {HD}" in text
    assert f"extra_libraries = {HVL}" in text
    assert "liberty =" not in text and "cell_lib =" not in text
    cfg = load_config(result.ofs_path, result.top_module)
    assert cfg.extra_libraries == (HVL,) and cfg.cell_lib == libraries.cell_lib
    monkeypatch.chdir(tmp_path)
    for step in (["init"], ["scan"], ["scan-check"], ["sim", "--scan"]):
        assert (
            main([*step, "--top", result.top_module, "-c", str(result.ofs_path)]) == 0
        ), step


@pytest.mark.integration
def test_the_tcl_command_takes_the_pdk_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    import faultflow.integrations.autombist as autombist
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    root = _root()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def run(config: Path, out: Path, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(autombist, "run_autombist_generate", run)
    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))
    with pytest.raises(Stop):
        bridge.call(
            "autombist_generate",
            *("-config", "cfg.yml", "-out", str(tmp_path / "out")),
            *("-pdk", "sky130A", "-std_cell_library", HD),
            *("-extra_libraries", HVL, "-pdk_root", str(root)),
        )
    assert dict(seen["design"])["extra_libraries"] == HVL
    assert seen["liberty"].liberty.name == "sky130_fd_sc_hd__tt_025C_1v80.lib"
