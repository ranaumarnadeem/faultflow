"""The whole scan flow on every standard-cell library of every installed PDK (Sky130's
seven, GF180MCU's and IHP SG13G2's): s27 synthesized onto the library
(``[design] pdk`` / ``std_cell_library``), scan inserted with the library's own scan
flops, checked, ATPG'd, and the exported patterns replayed on the library's own cell
models (scan_replay) unload what FaultFlow expects."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.cell_map import load_cell_map, lookup_cell
from faultflow.config import load_config
from scan_credit import credit_not_reproduced
from scan_replay import replay_on_cells
from wrap_flow import ACCUMULATOR

from .installed import installed, pdk_root_with

REPO = Path(__file__).resolve().parents[3]
TOP = "s27_bench"
LIBRARIES = [pytest.param(pdk, lib, id=lib) for pdk, lib in installed()]
# A cell instance in Yosys' write_verilog: "  <type> <name> (".
_INSTANCE = re.compile(r"^\s+([A-Za-z_]\w*)\s+(?:\\\S+|\w+)\s*\(", re.MULTILINE)


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")


RING = """module ring (input clk, input d0, input d1, output y);
  reg r0, r1, r2;
  always @(posedge clk) begin
    r0 <= d0 ^ r2;
    r1 <= r0 & d1;
    r2 <= r1 | d0;
  end
  assign y = r2 ^ r0;
endmodule
"""


def _flow(
    pdk: str,
    library: str,
    work: Path,
    sections: str,
    compose: tuple[str, ...] = (),
    *,
    netlist: Path = REPO / "tests/benchmarks/iscas89/s27.v",
    top: str = TOP,
) -> tuple[Any, Path]:
    """init, scan, scan-check, the `compose` steps and sim --scan on `netlist` (s27),
    on `library`: the config and the exported patterns."""
    from faultflow.cli import main

    root = pdk_root_with(pdk, library)
    if root is None:
        pytest.skip(f"{pdk} {library} is not installed")
    ofs = work / "s27.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\n"
        f"pdk = {pdk}\nstd_cell_library = {library}\npdk_root = {root}\n\n"
        f"{sections}",
        encoding="utf-8",
    )
    patterns = work / "patterns.json"
    steps = [["init"], ["scan"], ["scan-check"], *([s] for s in compose)]
    steps.append(["sim", "--scan", "--export-patterns", str(patterns)])
    for step in steps:
        assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    return load_config(ofs, top), patterns


@pytest.mark.integration
@pytest.mark.parametrize(("pdk", "library"), LIBRARIES)
def test_the_scan_flow_runs_and_replays_on_the_librarys_cells(
    pdk: str,
    library: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg, patterns = _flow(pdk, library, tmp_path, "[scan]\nchains = 1\n")

    # Every cell of the scanned chip is one of the library's the map models, and its
    # flops are scan flops.
    scanned = (cfg.output_dir / f"{TOP}_scan.v").read_text(encoding="utf-8")
    types = set(_INSTANCE.findall(scanned)) - {"module"}
    entries = load_cell_map(cfg.cell_lib)
    found = {t: lookup_cell(t, entries) for t in types}
    assert types and all(e and "node_type" in e for e in found.values()), found
    flops = [e for e in found.values() if e and e["node_type"] == "FF"]
    assert flops and all(e["ff"].get("scan") for e in flops), types
    assert json.loads(patterns.read_text(encoding="utf-8"))
    report = json.loads(cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
@pytest.mark.parametrize(("pdk", "library"), LIBRARIES)
def test_compressed_and_compacted_patterns_replay_on_the_librarys_cells(
    pdk: str,
    library: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
) -> None:
    """The decompressor and the compactor are the library's cells too: the composed
    chip replays on its models."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg, patterns = _flow(
        pdk,
        library,
        tmp_path,
        "[scan]\nchains = 3\n\n[compression]\nenabled = true\nchannels = 8\n\n"
        "[compaction]\nenabled = true\nchannels = 2\n",
        compose=("scan-compress", "scan-compact"),
    )
    assert json.loads(patterns.read_text(encoding="utf-8"))
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
@pytest.mark.parametrize(("pdk", "library"), LIBRARIES)
def test_flops_without_a_reset_scan_on_the_librarys_cells(
    pdk: str,
    library: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
) -> None:
    """Plain flops: the library's plain scan flop, or (a library of reset flops
    only, IHP SG13G2) its reset scan flop with the reset held inactive."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = tmp_path / "ring.v"
    source.write_text(RING, encoding="utf-8")
    cfg, patterns = _flow(
        pdk, library, tmp_path, "[scan]\nchains = 1\n", netlist=source, top="ring"
    )
    report = json.loads(cfg.coverage_json_path.read_text(encoding="utf-8"))
    assert report["summary"]["detected"] > 0
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


def _has_role(cfg: Any, role: str) -> bool:
    from faultflow.pdk.roles import PdkError, library_roles

    try:
        library_roles(json.loads(cfg.cell_lib.read_text(encoding="utf-8")))[role]
    except PdkError:
        return False
    return True


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["intest", "extest"])
@pytest.mark.parametrize(("pdk", "library"), LIBRARIES)
def test_the_ieee_1500_wrapper_runs_and_replays_on_the_librarys_cells(
    pdk: str,
    library: str,
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
) -> None:
    """The accumulator wrapped -- its boundary cells and, where the library has a
    falling-edge flop with a reset for the WIR's update stage, its WSP -- of the
    library's cells: INTEST and EXTEST patterns replay on them, INTEST with the
    environment at X, and every credit holds in the full scan protocol."""
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = pdk_root_with(pdk, library)
    if root is None:
        pytest.skip(f"{pdk} {library} is not installed")
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    design = (
        f"[design]\nnetlist = {rtl}\npdk = {pdk}\nstd_cell_library = {library}\n"
        f"pdk_root = {root}\n"
    )
    probe = tmp_path / "probe.ofs"
    probe.write_text(design, encoding="utf-8")
    wsp = _has_role(load_config(probe, "acc"), "ff_reset_negedge")
    ofs = tmp_path / "acc.ofs"
    ofs.write_text(
        f"{design}\n[wrap]\nenabled = true\nexclude = b[1]\n"
        + ("control = wsp\n\n[clocks]\ngroups = clk wrck\n" if wsp else "")
        + "\n[scan]\nchains = 1\n",
        encoding="utf-8",
    )
    patterns = tmp_path / f"{mode}.json"
    for step in (["init"], ["scan"], ["scan-check"]):
        assert main([*step, "--top", "acc", "-c", str(ofs)]) == 0, step
    run = [mode, "--export-patterns", str(patterns), "--top", "acc", "-c", str(ofs)]
    assert main(run) == 0
    cfg = load_config(ofs, "acc")
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    assert (manifest["wrapper"].get("control") == "wsp") == wsp
    assert json.loads(patterns.read_text(encoding="utf-8"))
    environment = ["en", "a[0]", "a[1]", "b[0]"] if mode == "intest" else []
    assert (
        replay_on_cells(cfg, patterns, tmp_path / "replay", unknown_inputs=environment)
        == []
    )
    campaign = "scan" if mode == "intest" else "scan_extest"
    assert credit_not_reproduced(cfg, patterns, campaign_type=campaign) == []


GATED = """module gated (input clk, input a, input b, input c, output y);
  reg r0, r1, r2, r3;
  wire gclk, en;
  assign en = a | r2;
  {cell} u_cg (.{clk}(clk), .{en}(en), .{te}(1'b0), .{gclk}(gclk));
  always @(posedge gclk) begin
    r0 <= a ^ r1;
    r1 <= r0 & b;
    r3 <= r1 | c;
  end
  always @(posedge clk) r2 <= r0 ^ c;
  assign y = r1 ^ r2 ^ r3;
endmodule
"""


@pytest.mark.integration
@pytest.mark.parametrize(("pdk", "library"), LIBRARIES)
def test_flops_its_clock_gate_gates_scan_compress_and_replay_on_the_librarys_cells(
    pdk: str,
    library: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
) -> None:
    """The library's clock gate with a test enable (role icg), its test enable tied
    off: scan insertion puts the scan enable on it, the gated flops scan on the root
    clock, and the compressed and compacted chip's patterns -- the gate's latch in
    the library's own model -- replay on its cells."""
    from faultflow.pdk.roles import PdkError, library_roles

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = pdk_root_with(pdk, library)
    if root is None:
        pytest.skip(f"{pdk} {library} is not installed")
    probe = tmp_path / "probe.ofs"
    probe.write_text(
        f"[design]\npdk = {pdk}\nstd_cell_library = {library}\npdk_root = {root}\n",
        encoding="utf-8",
    )
    cfg = load_config(probe, "gated")
    try:
        icg = library_roles(json.loads(cfg.cell_lib.read_text(encoding="utf-8")))["icg"]
    except PdkError:
        pytest.skip(f"{library} has no clock gate with a test enable")
    source = tmp_path / "gated.v"
    source.write_text(GATED.format(cell=icg.cell, **icg.pins), encoding="utf-8")
    cfg, patterns = _flow(
        pdk,
        library,
        tmp_path,
        "[scan]\nchains = 2\n\n[compression]\nenabled = true\nchannels = 4\n\n"
        "[compaction]\nenabled = true\nchannels = 1\n",
        compose=("scan-compress", "scan-compact"),
        netlist=source,
        top="gated",
    )
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    assert manifest["cell_count"] == 4 and not manifest["ineligible_ffs"]
    assert manifest["clock_gates"]["u_cg"]["test_pin"] == icg.pins["te"]
    assert json.loads(patterns.read_text(encoding="utf-8"))
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
