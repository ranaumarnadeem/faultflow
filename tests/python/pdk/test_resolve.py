"""A standard-cell library by name, from a PDK in the open_pdks layout
(``<root>/<pdk>/libs.ref/<lib>/{lib,verilog}``: Sky130, GF180MCU and IHP SG13G2 ship
this way, installed by ciel): its Liberty at a corner, one Verilog models file (the
library's primitives and cells), a cell map generated from the Liberty and cached, and
the cells synthesis must not use -- every cell the map doesn't model."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from faultflow.pdk.resolve import pdk_root, resolve_library
from faultflow.pdk.roles import PdkError


def _sky130_root() -> Path | None:
    """A PDK root with sky130A's hs and lp: ciel's store of every version fetched."""
    base = Path(os.environ.get("PDK_ROOT", Path.home() / ".ciel"))
    for root in [base, *sorted((base / "ciel/sky130/versions").glob("*"))]:
        if (root / "sky130A/libs.ref/sky130_fd_sc_hs/lib").is_dir() and (
            root / "sky130A/libs.ref/sky130_fd_sc_lp/lib"
        ).is_dir():
            return root
    return None


ROOT = _sky130_root()
needs_pdk = pytest.mark.skipif(
    ROOT is None, reason="no sky130A with hs and lp installed"
)


@pytest.mark.unit
def test_the_pdk_root_is_the_given_one_then_pdk_root_then_ciels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDK_ROOT", raising=False)
    assert pdk_root(tmp_path) == tmp_path
    assert pdk_root(None) == Path.home() / ".ciel"
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "x"))
    assert pdk_root(None) == tmp_path / "x"


@pytest.mark.unit
def test_a_library_the_pdk_does_not_have_is_refused_saying_how_to_install_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "sky130A/libs.ref").mkdir(parents=True)
    with pytest.raises(PdkError, match="sky130_fd_sc_hs.*ciel"):
        resolve_library(
            "sky130A", "sky130_fd_sc_hs", root=tmp_path, cache=tmp_path / "c"
        )


@needs_pdk
@pytest.mark.integration
def test_a_sky130_library_resolves_to_its_files_and_a_generated_map(
    tmp_path: Path, require_cpp_core: None
) -> None:
    assert ROOT is not None
    lib = resolve_library("sky130A", "sky130_fd_sc_hs", root=ROOT, cache=tmp_path)
    assert lib.liberty.name == "sky130_fd_sc_hs__tt_025C_1v80.lib"
    models = lib.verilog_models.read_text(encoding="utf-8")
    assert "primitive " in models and "module sky130_fd_sc_hs__nand2_1" in models
    cell_map = json.loads(lib.cell_map.read_text(encoding="utf-8"))
    assert cell_map["sky130_fd_sc_hs__nand2_*"]["gate_type"] == "NAND2"
    assert "$scanff_faultflow" in cell_map
    # The cells synthesis must not use: what the map doesn't model, by name.
    assert "sky130_fd_sc_hs__a222o_1" in lib.dont_use
    assert "sky130_fd_sc_hs__dlxtp_1" in lib.dont_use
    assert "sky130_fd_sc_hs__nand2_1" not in lib.dont_use
    # Cached: a second resolve reuses the map.
    stamp = lib.cell_map.stat().st_mtime_ns
    again = resolve_library("sky130A", "sky130_fd_sc_hs", root=ROOT, cache=tmp_path)
    assert again == lib and again.cell_map.stat().st_mtime_ns == stamp


@needs_pdk
@pytest.mark.integration
def test_a_library_without_a_tt_corner_takes_one_it_has_and_a_named_corner_wins(
    tmp_path: Path, require_cpp_core: None
) -> None:
    assert ROOT is not None
    lp = resolve_library("sky130A", "sky130_fd_sc_lp", root=ROOT, cache=tmp_path)
    assert lp.liberty.exists() and lp.liberty.parent.name == "lib"
    corner = lp.liberty.stem.split("__", 1)[1]
    named = resolve_library(
        "sky130A", "sky130_fd_sc_lp", root=ROOT, cache=tmp_path, corner=corner
    )
    assert named.liberty == lp.liberty
    with pytest.raises(PdkError, match="corner"):
        resolve_library(
            "sky130A", "sky130_fd_sc_lp", root=ROOT, cache=tmp_path, corner="xx_0C"
        )
