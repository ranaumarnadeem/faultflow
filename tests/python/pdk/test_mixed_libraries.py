"""A design on several libraries of one PDK ([design] extra_libraries): Sky130 hd
logic beside hvl cells the design instantiates -- level shifters, an isolation cell,
3.3 V flops -- as a Caravel user project does. Synthesis maps only onto the primary
library; every library's cells are in one cell map and one models file; each flop is
scanned with its own library's scan flop, on chains of that library alone."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.pdk.resolve import ResolvedLibrary, combine_libraries
from faultflow.pdk.roles import LIBRARY_KEY, PdkError, library_maps, library_roles
from scan_replay import replay_on_cells

from .installed import pdk_root_with

REPO = Path(__file__).resolve().parents[3]
HD_MAP = REPO / "cells/sky130/sky130_fd_sc_hd.json"
HVL = "sky130_fd_sc_hvl"
# A cell instance in Yosys' write_verilog: "  <type> <name> (".
_INSTANCE = re.compile(r"^\s+([A-Za-z_]\w*)\s+(\\\S+|\w+)\s*\(", re.MULTILINE)

MIXED = """module mixed (input clk, input rst_n, input a, input b, input c,
              input sleep_b, output y, output z);
  reg r0, r1;
  wire hv_a, hv_iso, hv_d, hv_e, hq0, hq1, hq2, lv_q;
  // The hvl domain, instantiated: level shifters in and out, an isolation cell,
  // hvl gates and flops of each scan shape (plain, reset, set).
  sky130_fd_sc_hvl__lsbuflv2hv_1 u_ls (.A(a), .X(hv_a));
  sky130_fd_sc_hvl__lsbuflv2hv_isosrchvaon_1 u_iso (.A(b), .SLEEP_B(sleep_b),
                                                     .X(hv_iso));
  sky130_fd_sc_hvl__xor2_1 u_hx (.A(hv_a), .B(hq2), .X(hv_d));
  sky130_fd_sc_hvl__dfxtp_1 u_h0 (.CLK(clk), .D(hv_d), .Q(hq0));
  sky130_fd_sc_hvl__dfrtp_1 u_h1 (.CLK(clk), .D(hq0), .RESET_B(rst_n), .Q(hq1));
  sky130_fd_sc_hvl__and2_1 u_ha (.A(hq1), .B(hv_iso), .X(hv_e));
  sky130_fd_sc_hvl__dfstp_1 u_h2 (.CLK(clk), .D(hv_e), .SET_B(rst_n), .Q(hq2));
  sky130_fd_sc_hvl__lsbufhv2lv_1 u_back (.A(hq2), .X(lv_q));
  // The hd domain, synthesized.
  always @(posedge clk) begin
    r0 <= lv_q ^ r1 ^ c;
    r1 <= r0 & b;
  end
  assign y = r0 | r1;
  assign z = lv_q;
endmodule
"""


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")


def _mixed_flow(
    work: Path, sections: str, compose: tuple[str, ...] = ()
) -> tuple[Any, Path]:
    from faultflow.cli import main

    ofs = _mixed_config(work, sections)
    patterns = work / "patterns.json"
    steps = [["init"], ["scan"], ["scan-check"], *([s] for s in compose)]
    steps.append(["sim", "--scan", "--export-patterns", str(patterns)])
    for step in steps:
        assert main([*step, "--top", "mixed", "-c", str(ofs)]) == 0, step
    return load_config(ofs, "mixed"), patterns


def _mixed_config(work: Path, sections: str) -> Path:
    root = pdk_root_with("sky130A", HVL)
    if root is None or pdk_root_with("sky130A", "sky130_fd_sc_hd") != root:
        pytest.skip("needs sky130A's hd and hvl in one PDK root")
    source = work / "mixed.v"
    source.write_text(MIXED, encoding="utf-8")
    ofs = work / "mixed.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {source}\npdk = sky130A\n"
        f"std_cell_library = sky130_fd_sc_hd\nextra_libraries = {HVL}\n"
        f"pdk_root = {root}\n\n{sections}",
        encoding="utf-8",
    )
    return ofs


def _instances(verilog: Path) -> dict[str, str]:
    """Instance name -> cell type, of a Yosys gate-level netlist."""
    text = verilog.read_text(encoding="utf-8")
    return {
        name.lstrip("\\"): cell
        for cell, name in _INSTANCE.findall(text)
        if cell != "module"
    }


@pytest.mark.integration
def test_each_flop_scans_with_its_own_librarys_scan_flop_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg, patterns = _mixed_flow(tmp_path, "[scan]\nchains = 2\nhold = rst_n:1\n")
    assert cfg.extra_libraries == (HVL,)

    cells = _instances(cfg.output_dir / "mixed_scan.v")
    # The hvl cells the design instantiates are kept, its flops scanned by hvl's own.
    assert cells["u_ls"] == "sky130_fd_sc_hvl__lsbuflv2hv_1"
    assert cells["u_iso"] == "sky130_fd_sc_hvl__lsbuflv2hv_isosrchvaon_1"
    assert cells["u_back"] == "sky130_fd_sc_hvl__lsbufhv2lv_1"
    assert cells["u_h0"] == "sky130_fd_sc_hvl__sdfxtp_1"
    assert cells["u_h1"] == "sky130_fd_sc_hvl__sdfrtp_1"
    assert cells["u_h2"] == "sky130_fd_sc_hvl__sdfstp_1"
    # The synthesized logic is hd's alone.
    synthesized = [cell for name, cell in cells.items() if not name.startswith("u_")]
    assert synthesized and all(c.startswith("sky130_fd_sc_hd__") for c in synthesized)
    assert Counter(synthesized)["sky130_fd_sc_hd__sdfxtp_1"] == 2

    # A chain is of one library.
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    chains = [
        {"hvl" if name.startswith("u_h") else "hd" for name in chain["cells"]}
        for chain in manifest["chains"]
    ]
    assert sorted(map(sorted, chains)) == [["hd"], ["hvl"]]

    report = json.loads(cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
def test_compressed_and_compacted_mixed_design_replays_on_both_librarys_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg, patterns = _mixed_flow(
        tmp_path,
        "[scan]\nchains = 3\nhold = rst_n:1\n\n[compression]\nenabled = true\n"
        "channels = 8\n\n[compaction]\nenabled = true\nchannels = 2\n",
        compose=("scan-compress", "scan-compact"),
    )
    assert json.loads(patterns.read_text(encoding="utf-8"))
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
def test_too_few_chains_for_both_libraries_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ofs = _mixed_config(tmp_path, "[scan]\nchains = 1\nhold = rst_n:1\n")
    assert main(["init", "--top", "mixed", "-c", str(ofs)]) == 0
    with pytest.raises(SystemExit):
        main(["scan", "--top", "mixed", "-c", str(ofs)])
    assert "split by cell library (2)" in capsys.readouterr().err


@pytest.mark.integration
def test_gate_level_verilog_synthesizes_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    """A netlist of library cells is read with the library's cells: synthesized
    again, it comes back cell for cell."""
    from faultflow.runner.runner import Runner

    monkeypatch.chdir(REPO)  # the shipped library's paths are the repo's

    def synth(netlist: Path, out: str) -> Path:
        ofs = tmp_path / f"{out}.ofs"
        ofs.write_text(
            f"[design]\nnetlist = {netlist}\noutput_root = {tmp_path / out}\n",
            encoding="utf-8",
        )
        cfg = load_config(ofs, "s27_bench")
        Runner(cfg).synth()
        return cfg.intermediate_dir / "s27_bench_gate.v"

    gate = synth(REPO / "tests/benchmarks/iscas89/s27.v", "first")
    regate = synth(gate, "again")
    cells = Counter(_instances(gate).values())
    assert cells and Counter(_instances(regate).values()) == cells


def _library(tmp_path: Path, name: str, cell_map: dict[str, Any], models: str) -> Any:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "map.json").write_text(json.dumps(cell_map), encoding="utf-8")
    (folder / "models.v").write_text(models, encoding="utf-8")
    return ResolvedLibrary(
        "sky130A",
        name,
        folder / "x.lib",
        folder / "models.v",
        folder / "map.json",
        (),
    )


_GATE = {
    "node_type": "GATE",
    "gate_type": "INV",
    "inputs": ["A"],
    "outputs": {"Y": "Y"},
}


@pytest.mark.unit
def test_combined_libraries_tag_the_extras_entries_and_join_the_models(
    tmp_path: Path,
) -> None:
    hd = _library(
        tmp_path,
        "lib_a",
        {"$scanff_faultflow": {"node_type": "FF"}, "lib_a__inv_*": _GATE},
        "module lib_a__inv_1 (); endmodule\n",
    )
    hvl = _library(
        tmp_path,
        "lib_b",
        {"$scanff_faultflow": {"node_type": "FF"}, "lib_b__inv_*": _GATE},
        "module lib_b__inv_1 (); endmodule\n",
    )
    cell_map, models = combine_libraries(hd, [hvl], cache=tmp_path / "c")
    combined = json.loads(cell_map.read_text(encoding="utf-8"))
    assert combined["lib_a__inv_*"] == _GATE
    assert combined["lib_b__inv_*"] == {**_GATE, LIBRARY_KEY: "lib_b"}
    assert list(combined) == ["$scanff_faultflow", "lib_a__inv_*", "lib_b__inv_*"]
    split = library_maps(combined)
    assert split["lib_b"] == {"lib_b__inv_*": _GATE}
    assert "lib_b__inv_*" not in split[""]
    text = models.read_text(encoding="utf-8")
    assert "module lib_a__inv_1" in text and "module lib_b__inv_1" in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("b_map", "b_models", "refused"),
    [
        ({"lib_a__inv_*": _GATE}, "", "both have a cell lib_a__inv_"),
        ({"lib_*": _GATE}, "", "name the same cells"),
        ({}, "module lib_a__inv_1 (); endmodule\n", "Verilog model lib_a__inv_1"),
    ],
)
def test_libraries_naming_the_same_cell_are_refused(
    tmp_path: Path, b_map: dict[str, Any], b_models: str, refused: str
) -> None:
    a = _library(
        tmp_path,
        "lib_a",
        {"lib_a__inv_*": _GATE},
        "module lib_a__inv_1 (); endmodule\n",
    )
    b = _library(tmp_path, "lib_b", b_map, b_models)
    with pytest.raises(PdkError, match=refused):
        combine_libraries(a, [b], cache=tmp_path / "c")


@pytest.mark.unit
def test_libraries_of_two_pdks_are_refused(tmp_path: Path) -> None:
    a = _library(tmp_path, "lib_a", {}, "")
    b = ResolvedLibrary(
        "gf180mcuD", "lib_b", a.liberty, a.verilog_models, a.cell_map, ()
    )
    with pytest.raises(PdkError, match="of one PDK"):
        combine_libraries(a, [b], cache=tmp_path / "c")


@pytest.mark.unit
def test_extra_libraries_need_the_primary_library(tmp_path: Path) -> None:
    ofs = tmp_path / "x.ofs"
    ofs.write_text(f"[design]\nextra_libraries = {HVL}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="extra_libraries needs"):
        load_config(ofs, "top")


def _two_library_map(*, extra_scan: bool) -> dict[str, Any]:
    """The shipped HD map, and a copy of its logic as library ``lib2`` -- without its
    scan flops unless ``extra_scan``."""
    hd = json.loads(HD_MAP.read_text(encoding="utf-8"))
    combined = dict(hd)
    for key, entry in hd.items():
        if key.startswith(("$", "\\")) or not isinstance(entry, dict):
            continue
        if not extra_scan and (entry.get("ff") or {}).get("scan"):
            continue
        variants = entry.get("variants")
        copied = {**entry, LIBRARY_KEY: "lib2"}
        if isinstance(variants, list):
            copied["variants"] = [
                v.replace("sky130_fd_sc_hd", "lib2") for v in variants
            ]
        combined[key.replace("sky130_fd_sc_hd", "lib2")] = copied
    return combined


def _netlist(flops: dict[str, str]) -> dict[str, Any]:
    """Flops ``name -> type`` on clock net 2, each D from input net 3."""
    cells = {}
    for index, (name, cell_type) in enumerate(sorted(flops.items())):
        cells[name] = {
            "type": cell_type,
            "parameters": {},
            "attributes": {},
            "port_directions": {},
            "connections": {"CLK": [2], "D": [3], "Q": [10 + index]},
        }
    outputs = {
        f"q{i}": {"direction": "output", "bits": [10 + i]} for i in range(len(flops))
    }
    return {
        "modules": {
            "t": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "d": {"direction": "input", "bits": [3]},
                    **outputs,
                },
                "cells": cells,
                "netnames": {},
            }
        }
    }


@pytest.mark.unit
def test_an_extra_librarys_flop_carries_its_library_onto_a_chain_of_its_own(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.scan.stitch import LIBRARY_PARAMETER, stitch_scan_json

    cell_map = tmp_path / "map.json"
    cell_map.write_text(json.dumps(_two_library_map(extra_scan=True)))
    netlist = tmp_path / "n.json"
    netlist.write_text(
        json.dumps(
            _netlist(
                {
                    "a": "sky130_fd_sc_hd__dfxtp_1",
                    "b": "lib2__dfxtp_1",
                    "c": "sky130_fd_sc_hd__dfxtp_1",
                }
            )
        )
    )
    result = stitch_scan_json(netlist, cell_map, "t", tmp_path / "out.json", 2)
    assert sorted(sorted(c.instance for c in ch.cells) for ch in result.chains) == [
        ["a", "c"],
        ["b"],
    ]
    cells = json.loads((tmp_path / "out.json").read_text())["modules"]["t"]["cells"]
    assert cells["b"]["parameters"] == {LIBRARY_PARAMETER: "lib2"}
    assert cells["a"]["parameters"] == {} and cells["c"]["parameters"] == {}


@pytest.mark.unit
def test_an_extra_librarys_flop_with_no_scan_flop_of_its_own_stays_out_of_scan(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.scan.stitch import stitch_scan_json

    cell_map = tmp_path / "map.json"
    cell_map.write_text(json.dumps(_two_library_map(extra_scan=False)))
    netlist = tmp_path / "n.json"
    netlist.write_text(
        json.dumps(_netlist({"a": "sky130_fd_sc_hd__dfxtp_1", "b": "lib2__dfxtp_1"}))
    )
    result = stitch_scan_json(netlist, cell_map, "t", tmp_path / "out.json", 1)
    assert [c.instance for c in result.cells] == ["a"]
    assert [(f.instance, f.reason) for f in result.ineligible_ffs] == [
        ("b", "no_library_scan_cell")
    ]


@pytest.mark.unit
def test_the_techmap_maps_each_librarys_cells_onto_its_own(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.scan.techmap import render_scan_techmap, scan_techmap_config

    cell_map = tmp_path / "map.json"
    cell_map.write_text(json.dumps(_two_library_map(extra_scan=True)))
    cfg = scan_techmap_config(cell_map)
    assert [e.library for e in cfg.extra] == ["lib2"]
    text = render_scan_techmap(cfg)
    modules = re.findall(r"module (\w+) \(", text)
    assert len(modules) == len(set(modules)) == 12
    # Each module maps only the generic cells of its own library.
    for body in text.split("endmodule")[:-1]:
        guard = re.search(r'FAULTFLOW_LIBRARY != "(\w*)"', body)
        cell = re.search(r"(\w+) _TECHMAP_REPLACE_", body)
        assert guard and cell, body
        assert cell[1].startswith(
            "lib2__" if guard[1] == "lib2" else "sky130_fd_sc_hd__"
        )
    # One library alone: no guards.
    assert "FAULTFLOW_LIBRARY" not in render_scan_techmap(scan_techmap_config(HD_MAP))


@pytest.mark.unit
def test_the_primary_librarys_roles_ignore_the_extra_librarys_cells(
    require_cpp_core: None,
) -> None:
    roles = library_roles(_two_library_map(extra_scan=True))
    assert roles.name == "sky130_fd_sc_hd"
    for role in roles.names():
        assert roles[role].cell.startswith("sky130_fd_sc_hd__"), role


@pytest.mark.integration
def test_a_shell_profile_names_its_extra_libraries_after_a_plus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    from faultflow.project.profiles import get_profile

    root = pdk_root_with("sky130A", HVL)
    if root is None or pdk_root_with("sky130A", "sky130_fd_sc_hd") != root:
        pytest.skip("needs sky130A's hd and hvl in one PDK root")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    profile = get_profile(f"sky130A/sky130_fd_sc_hd+{HVL}", pdk_root=root)
    assert profile.extra_libraries == (HVL,)
    assert [p.name for p in profile.liberty_files] == [
        "sky130_fd_sc_hd__tt_025C_1v80.lib",
        "sky130_fd_sc_hvl__tt_025C_3v30.lib",
        "sky130_fd_sc_hvl__tt_025C_3v30_lv1v80.lib",
    ]
    assert profile.liberty == profile.liberty_files[0]
    entries = json.loads(profile.cell_map.read_text(encoding="utf-8"))
    assert entries["sky130_fd_sc_hvl__lsbufhv2lv_1"][LIBRARY_KEY] == HVL
    assert profile.supports_physical_scan


@pytest.mark.integration
def test_hvls_level_shifters_are_in_its_map_from_the_dual_supply_liberty(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.pdk.resolve import resolve_library

    root = pdk_root_with("sky130A", HVL)
    if root is None:
        pytest.skip("needs sky130A's hvl")
    hvl = resolve_library("sky130A", HVL, root=root, cache=tmp_path)
    assert [p.name for p in hvl.liberty_files] == [
        "sky130_fd_sc_hvl__tt_025C_3v30.lib",
        "sky130_fd_sc_hvl__tt_025C_3v30_lv1v80.lib",
    ]
    cell_map = json.loads(hvl.cell_map.read_text(encoding="utf-8"))
    shifters = {
        key: (entry["gate_type"], entry["inputs"])
        for key, entry in cell_map.items()
        if "__lsbuf" in key
    }
    assert len(shifters) == 8, shifters
    assert shifters["sky130_fd_sc_hvl__lsbuflv2hv_isosrchvaon_*"] == (
        "AND2",
        ["A", "SLEEP_B"],
    )
    assert shifters["sky130_fd_sc_hvl__lsbufhv2lv_1"] == ("BUF", ["A"])
