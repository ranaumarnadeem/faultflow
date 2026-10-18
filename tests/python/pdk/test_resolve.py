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


@pytest.mark.unit
def test_a_base_cell_the_models_never_define_is_its_sized_variants_model() -> None:
    from faultflow.pdk.resolve import _missing_base_models

    text = (
        "`ifndef LIB__INV_1_V\n`define LIB__INV_1_V\n"
        "module lib__inv_1 (Y, A);\n  output Y; input A;\n  not (Y, A);\nendmodule\n"
        "`endif  // LIB__INV_1_V\n"
        "`ifndef LIB__INV_2_V\n`define LIB__INV_2_V\n"
        "module lib__inv_2 (Y, A);\n  output Y; input A;\n"
        "  lib__inv base (\n    .Y(Y), .A(A)\n  );\nendmodule\n"
        "`endif  // LIB__INV_2_V\n"
    )
    added = _missing_base_models(text)
    assert "module lib__inv (Y, A);" in added and "not (Y, A);" in added
    assert "LIB__INV_BASE_V" in added and "module lib__inv_1" not in added
    # Nothing to add once every base is defined.
    assert _missing_base_models(text + added) == ""


@needs_pdk
@pytest.mark.integration
def test_every_cell_of_a_librarys_models_file_is_defined(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """open_pdks' sky130_fd_sc_lp.v has sized cells wrapping base cells it never
    defines; the joined models file defines them, and iverilog elaborates every
    cell."""
    import shutil
    import subprocess

    if shutil.which("iverilog") is None:
        pytest.skip("needs iverilog")
    assert ROOT is not None
    lp = resolve_library("sky130A", "sky130_fd_sc_lp", root=ROOT, cache=tmp_path)
    text = lp.verilog_models.read_text(encoding="utf-8")
    import re

    defined = set(re.findall(r"^\s*module\s+(\w+)", text, re.M))
    wrapped = set(re.findall(r"^\s*(\w+)\s+base\s*\(", text, re.M))
    assert wrapped and wrapped <= defined, sorted(wrapped - defined)[:5]
    top = tmp_path / "top.v"
    cells = sorted(c for c in defined if c.startswith("sky130_fd_sc_lp__"))
    top.write_text(
        "module top;\n"
        + "".join(f"  {c} u{i} ();\n" for i, c in enumerate(cells))
        + "endmodule\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-DFUNCTIONAL",
            "-s",
            "top",
            "-o",
            os.devnull,
            str(top),
            str(lp.verilog_models),
        ],
        capture_output=True,
        text=True,
    )
    assert "Unknown module type" not in result.stderr, result.stderr[-2000:]


@pytest.mark.unit
def test_a_bare_word_after_endif_becomes_a_comment(tmp_path: Path) -> None:
    """Some open_pdks versions' sky130_fd_sc_hd.v end a guard with
    "`endif SKY130_..._V", which iverilog refuses."""
    from faultflow.pdk.resolve import _BARE_ENDIF

    text = "`ifndef A_V\n`define A_V\n`endif A_V\n`endif  // B_V\n  `endif\n"
    fixed = _BARE_ENDIF.sub(r"\1 // \2", text)
    assert (
        fixed == "`ifndef A_V\n`define A_V\n`endif // A_V\n`endif  // B_V\n  `endif\n"
    )


@pytest.mark.unit
def test_the_install_hint_names_ciels_family_of_the_pdk() -> None:
    from faultflow.pdk.resolve import ciel_family

    assert [
        ciel_family(p)
        for p in ("sky130A", "sky130B", "gf180mcuD", "ihp-sg13g2", "ihp-sg13cmos5l")
    ] == ["sky130", "sky130", "gf180mcu", "ihp-sg13", "ihp-sg13"]
    assert ciel_family("asap7") == "asap7"


@pytest.mark.unit
def test_liberty_files_are_found_by_either_naming_and_the_named_supply_wins(
    tmp_path: Path,
) -> None:
    """Sky130 and GF180MCU name a corner's Liberty <library>__<corner>.lib, IHP
    <library>_<corner>.lib; a library named for its supply (mcu7t5v0) takes that
    corner, not the first nominal one."""
    from faultflow.pdk.resolve import _corner

    gf = tmp_path / "gf"
    gf.mkdir()
    for corner in ("tt_025C_1v80", "tt_025C_3v30", "tt_025C_5v00", "ss_125C_4v50"):
        (gf / f"gf180mcu_fd_sc_mcu7t5v0__{corner}.lib").touch()
    assert _corner(gf, "gf180mcu_fd_sc_mcu7t5v0", None).stem.endswith("tt_025C_5v00")
    ihp = tmp_path / "ihp"
    ihp.mkdir()
    for corner in ("fast_1p32V_m40C", "typ_1p20V_25C", "typ_1p50V_25C"):
        (ihp / f"sg13g2_stdcell_{corner}.lib").touch()
    assert (
        _corner(ihp, "sg13g2_stdcell", None).name == "sg13g2_stdcell_typ_1p20V_25C.lib"
    )
    assert _corner(ihp, "sg13g2_stdcell", "typ_1p50V_25C").name.endswith(
        "1p50V_25C.lib"
    )
    with pytest.raises(PdkError, match="fast_1p32V_m40C"):
        _corner(ihp, "sg13g2_stdcell", "slow")
    # A sibling library's files (hd beside hdll) are not this library's corners.
    hd = tmp_path / "hd"
    hd.mkdir()
    (hd / "sky130_fd_sc_hdll__tt_025C_1v80.lib").touch()
    with pytest.raises(PdkError, match="no Liberty"):
        _corner(hd, "sky130_fd_sc_hd", None)
