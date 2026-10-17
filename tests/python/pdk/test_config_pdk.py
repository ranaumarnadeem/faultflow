"""``[design] pdk`` and ``std_cell_library`` name the library a design is synthesized
and tested on; the Liberty, the Verilog models and the cell map come from the PDK
(faultflow.pdk.resolve) unless the config names them, and synthesis keeps off every
cell the map doesn't model (``dfflibmap`` / ``abc`` ``-dont_use``)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config

from .test_resolve import ROOT as PDK_ROOT
from .test_resolve import needs_pdk

REPO = Path(__file__).resolve().parents[3]
HS = f"pdk = sky130A\nstd_cell_library = sky130_fd_sc_hs\npdk_root = {PDK_ROOT}\n"


def _ofs(tmp_path: Path, design: str) -> Path:
    path = tmp_path / "config.ofs"
    path.write_text(f"[design]\nnetlist = design.v\n{design}\n", encoding="utf-8")
    return path


@pytest.mark.unit
def test_a_library_needs_its_pdk(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="pdk"):
        load_config(_ofs(tmp_path, "std_cell_library = sky130_fd_sc_hs"), "top")


@pytest.mark.unit
def test_without_a_library_the_design_is_sky130_hd_as_always(tmp_path: Path) -> None:
    cfg = load_config(_ofs(tmp_path, ""), "top")
    assert cfg.cell_lib == Path("cells/sky130/sky130_fd_sc_hd.json")
    assert cfg.dont_use == ()
    assert (cfg.pdk, cfg.std_cell_library) == ("", "")


@needs_pdk
@pytest.mark.integration
def test_a_named_library_resolves_from_the_pdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load_config(
        _ofs(
            tmp_path,
            HS + "dont_use = sky130_fd_sc_hs__nand4*",
        ),
        "top",
    )
    assert (
        cfg.liberty is not None
        and cfg.liberty.name == "sky130_fd_sc_hs__tt_025C_1v80.lib"
    )
    assert (
        cfg.verilog_models is not None and "sky130_fd_sc_hs" in cfg.verilog_models.name
    )
    assert json.loads(cfg.cell_lib.read_text(encoding="utf-8"))[
        "sky130_fd_sc_hs__nand2_*"
    ]
    assert "sky130_fd_sc_hs__a222o_1" in cfg.dont_use
    assert "sky130_fd_sc_hs__nand4*" in cfg.dont_use  # the user's own, as given
    # A path the config names wins over the PDK's.
    own = load_config(
        _ofs(
            tmp_path,
            HS + f"cell_lib = {REPO / 'cells/sky130/sky130_fd_sc_hd.json'}",
        ),
        "top",
    )
    assert own.cell_lib == REPO / "cells/sky130/sky130_fd_sc_hd.json"
    assert own.liberty == cfg.liberty


@needs_pdk
@pytest.mark.integration
def test_synthesis_keeps_off_the_cells_the_map_does_not_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    from faultflow.runner.runner import Runner

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load_config(
        _ofs(
            tmp_path,
            f"pdk = sky130A\nstd_cell_library = sky130_fd_sc_hs\npdk_root = {PDK_ROOT}",
        ),
        "top",
    )
    script = Runner(cfg)._rendered_yosys_script(tmp_path / "design.v")
    dfflibmap = next(
        line for line in script.splitlines() if line.startswith("dfflibmap")
    )
    abc = next(line for line in script.splitlines() if line.startswith("abc"))
    for line in (dfflibmap, abc):
        assert f"-liberty {cfg.liberty}" in line
        assert "-dont_use sky130_fd_sc_hs__a222o_1" in line
        assert "-dont_use sky130_fd_sc_hs__nand2_1" not in line


@needs_pdk
@pytest.mark.integration
def test_a_design_synthesizes_and_is_tested_on_another_sky130_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    """c432 on sky130_fd_sc_hs: every cell Yosys maps it to is an hs cell the map
    models, and combinational ATPG grades it."""
    if shutil.which("yosys") is None:
        pytest.skip("needs Yosys")
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = REPO / "tests/benchmarks/iscas85/c432.v"
    ofs = tmp_path / "config.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {source}\n{HS}\n[atpg]\nmode = comb\nmax_rounds = 5\n",
        encoding="utf-8",
    )
    assert main(["init", "--top", "c432", "-c", str(ofs)]) == 0
    assert main(["sim", "--top", "c432", "-c", str(ofs)]) == 0
    cfg = load_config(ofs, "c432")
    synthesized = cfg.intermediate_dir / "c432.json"
    netlist = json.loads(synthesized.read_text(encoding="utf-8"))
    types = {c["type"] for c in netlist["modules"]["c432"]["cells"].values()}
    assert types and all(t.startswith("sky130_fd_sc_hs__") for t in types), types
    assert not types & set(cfg.dont_use)
    report = json.loads(cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
