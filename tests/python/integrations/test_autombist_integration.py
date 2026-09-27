"""Real-Yosys integration test for the autoMBIST synthesis pipeline.

Loads a checked-in, real (captured from a live `autombist generate
--emit-manifest` run, then trimmed/scrubbed of machine-specific paths)
manifest + RTL fixture and drives `synthesize_from_manifest` (not
`run_autombist_generate` -- no live autoMBIST invocation is available or
needed against an already-captured manifest).

Departs from `tests/python/project/test_assemble.py`'s own inline-RTL local
convention deliberately: faithfully hand-writing a realistic
`manifest.json` (the real `hierarchy_hint`/`category`/`parameters`/`sources`/
`geometry` shape) as an inline Python dict literal would be more error-prone
and less representative than reusing a real, tiny, already-captured example.
`tests/fixtures/scan_protocol/single_chain/` is the direct precedent for a
checked-in manifest+companion-data fixture directory in this codebase.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.integrations.autombist import (
    load_autombist_manifest,
    plan_blocks,
    synthesize_from_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.integration
def test_blackbox_design_runs_the_full_scan_flow_under_the_default_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    require_cpp_core: None,
) -> None:
    """init -> scan -> scan-check -> sim --scan on a design whose memory is an
    explicitly blackboxed instance ([blackbox] instances = u_sram, cell type
    absent from the cell map), with NO `unsupported_cells = blackbox`.

    Every netlist load on the scan path used to ignore the blackbox list, so
    under the default unsupported_cells = fail scan-check died with
    "Unsupported cell: <memory type>" and sim --scan (which requires a passing
    scan-check) was unreachable for every design with a blackbox."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    from faultflow.cli import main

    manifest = load_autombist_manifest(FIXTURE / "manifest.json")
    memory = next(i for i in manifest.instances if i.hierarchical_path == "u_sram")
    result = synthesize_from_manifest(
        manifest,
        out=tmp_path / "synth",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )
    top = result.top_module
    ofs = result.ofs_path
    assert result.blackbox_instances == ("u_sram",)
    assert "unsupported_cells" not in ofs.read_text(encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    for step in (["init"], ["scan"], ["scan-check"], ["sim", "--scan"]):
        assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    assert "scan-check PASS" in capsys.readouterr().out

    # Scan insertion must keep the blackbox instance under its own name and
    # type, or the [blackbox] list would no longer match anything.
    out_dir = tmp_path / "output" / top
    scanned = json.loads((out_dir / f"{top}_scan.json").read_text(encoding="utf-8"))
    assert scanned["modules"][top]["cells"]["u_sram"]["type"] == memory.module_type

    report = json.loads(
        (out_dir / ".faultflow/intermediate/coverage_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["summary"]["denominator"] > 0
    assert report["summary"]["detected"] > 0
    assert report["policy"]["blackbox_instances"] == ["u_sram"]
    assert report["policy"]["blackbox_boundary"] == "opaque"


@pytest.mark.integration
def test_synthesize_from_manifest_end_to_end(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    manifest = load_autombist_manifest(FIXTURE / "manifest.json")
    assert manifest.top_module == "input_demo_8x16_scn4m_mbist"
    assert len(manifest.instances) == 2

    blocks = plan_blocks(manifest)
    assert len(blocks) == 1
    assert blocks[0].module == "march_c_top"
    assert blocks[0].instance_paths == ("u_algo_top",)

    # synthesize_from_manifest itself runs a Yosys `check -assert` on the
    # composed netlist (_check_composed_netlist_drivers in autombist.py) and
    # raises AssembleError on any "used but has no driver" problem -- so
    # simply reaching this line without an exception already proves this
    # fixture's composed netlist is driver-clean. It has had a dangling
    # controller clock before compose_soc's union-find rewrite (a MERGE bug:
    # the controller's own clk port aliases another port internally, and the
    # OLD flat per-instance remap dict kept only the last one written).
    result = synthesize_from_manifest(
        manifest, out=tmp_path, liberty=SKY130_LIBERTY, cell_lib=SKY130_CELL_MAP
    )

    assert result.top_module == "input_demo_8x16_scn4m_mbist"
    assert result.blackbox_instances == ("u_sram",)
    assert result.block_count == 1
    assert result.instance_counts == {"memory": 1, "mbist_controller": 1}
    assert result.ofs_path.exists()
    assert result.composed_json_path.exists()

    # The .ofs round-trips through load_config with the memory blackboxed.
    cfg = load_config(result.ofs_path, "input_demo_8x16_scn4m_mbist")
    assert str(cfg.netlist) == str(result.composed_json_path)
    assert list(cfg.blackbox_instances) == ["u_sram"]

    # Composed JSON: both instances spliced in (u_sram's real blackbox cell
    # survives as its own cell since memories are never synthesized/spliced --
    # only "separate" blocks get spliced via compose_soc), no leftover glue
    # instance cell for the block, no $scopeinfo debug cells.
    composed = json.loads(result.composed_json_path.read_text(encoding="utf-8"))
    top = composed["modules"][result.top_module]
    cell_names = set(top["cells"].keys())
    assert "u_algo_top" not in cell_names  # instance cell removed by compose_soc
    assert any(
        name.startswith("u_algo_top__") for name in cell_names
    ), "expected u_algo_top's spliced cells"
    assert "u_sram" in cell_names  # memory blackbox cell untouched
    assert not any("scopeinfo" in name for name in cell_names)

    # The composed netlist loads through the C++ core (proves it's not just
    # well-formed JSON but a genuinely simulatable netlist).
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    # "blackbox" (not "fail"): u_sram's cell type is the memory's own module
    # name, not a sky130 cell -- Policy 1's blackbox path, matching the
    # written .ofs's [blackbox] section.
    rows = core.list_site_keys(
        str(result.composed_json_path), str(SKY130_CELL_MAP), "blackbox"
    )
    assert rows, "expected at least one fault site in the composed netlist"


@pytest.mark.integration
def test_synthesize_from_manifest_tolerates_a_tied_off_separate_block_output(
    tmp_path: Path,
) -> None:
    """Regression for a real bug: a "separate" block whose instantiation ties
    off one output with explicit empty parens (`.port()`) -- the shape a
    code-generated wrapper emits for every declared port whether wired or not,
    e.g. autoMBIST's `onchip_row_repair_analyzer` leaving `repair_load_done`
    unconnected whenever `redundancy.onchip_repair_persistence` is off (the
    common case) -- used to raise `AssembleError("port ... width mismatch vs
    instance connections")` from `compose_soc`'s `_build_remap`. Confirmed by
    direct inspection of real Yosys JSON output: this shape is a PRESENT
    "connections" key with an EMPTY list (`[]`), not an absent key (that's the
    OTHER real shape, produced when the port is omitted from the instantiation
    entirely -- see the key-absent variant in test_assemble.py), so a guard
    that only checks `is None` misses it. Fixture-free rather than a second
    checked-in autoMBIST capture: reproducible from a few lines of RTL."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    src = tmp_path / "src"
    src.mkdir()
    (src / "mem_stub.v").write_text(
        "(* blackbox *)\n"
        "module tiny_mem(input clk, input we, input [2:0] addr, "
        "input [3:0] din, output [3:0] dout);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    # y and unused_out must be genuinely distinct nets (not both trivial
    # aliases of a single input), or Yosys collapses the whole block to zero
    # internal cells and there is nothing left for compose_soc to splice.
    (src / "leaf_block.v").write_text(
        "module leaf_block(input clk, input a, input b, output y, "
        "output unused_out);\n"
        "  assign y = a & b;\n"
        "  assign unused_out = ~(a & b);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (src / "wrapper.v").write_text(
        "module top_wrap(input clk, input a, input b, input we, "
        "input [2:0] addr, input [3:0] din, output [3:0] dout, output y);\n"
        "  leaf_block u_leaf(.clk(clk), .a(a), .b(b), .y(y), "
        ".unused_out());\n"
        "  tiny_mem u_mem(.clk(clk), .we(we), .addr(addr), .din(din), "
        ".dout(dout));\n"
        "endmodule\n",
        encoding="utf-8",
    )

    manifest_data = {
        "format": "autombist_instance_manifest",
        "schema_version": "1.0.0",
        "top_module": "top_wrap",
        "sources": {"wrapper": "wrapper.v"},
        "instances": [
            {
                "category": "memory",
                "hierarchical_path": "u_mem",
                "hierarchy_hint": "blackbox",
                "instance_name": "u_mem",
                "module_type": "tiny_mem",
                "sources": ["mem_stub.v"],
            },
            {
                "category": "instrument",
                "hierarchical_path": "u_leaf",
                "hierarchy_hint": "separate",
                "instance_name": "u_leaf",
                "module_type": "leaf_block",
                "sources": ["leaf_block.v"],
            },
        ],
    }
    manifest_path = src / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_autombist_manifest(manifest_path)
    result = synthesize_from_manifest(
        manifest,
        out=tmp_path / "out",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )

    composed = json.loads(result.composed_json_path.read_text(encoding="utf-8"))
    top = composed["modules"][result.top_module]
    cell_names = set(top["cells"].keys())
    assert "u_leaf" not in cell_names  # instance cell removed by compose_soc
    assert any(
        name.startswith("u_leaf__") for name in cell_names
    ), "expected u_leaf's spliced cells"
    assert "u_mem" in cell_names  # memory blackbox cell untouched


@pytest.mark.integration
def test_synthesize_from_manifest_tolerates_a_block_clock_aliased_to_another_port(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Regression for a real bug: a "separate" block whose `clk` input is ALSO
    exposed, aliased, on a second output port (e.g. an MBIST controller that
    passes its own clock through to drive the memory it controls) -- the
    exact shape a real self-repair autoMBIST config hit for real
    (`march_c_top`'s `clk`/`sram_clk0`, confirmed via `yosys ... check` on a
    live-generated composed netlist: 13 "used but has no driver" problems
    before this fix, 0 after). Real Yosys synthesis merges the block's own
    `clk` and `pass_clk` ports to the SAME internal bit id (a trivial wire
    alias); the OLD flat per-instance remap dict kept only whichever port was
    processed last, silently leaving either the block's own flip-flop or the
    memory it feeds with an undriven clock. Fixture-free: reproducible from a
    few lines of RTL with a real flip-flop (so Yosys can't optimize the
    aliasing away entirely)."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    src = tmp_path / "src"
    src.mkdir()
    (src / "mem_stub.v").write_text(
        "(* blackbox *)\n"
        "module tiny_mem(input clk, input we, input [2:0] addr, "
        "input [3:0] din, output [3:0] dout);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (src / "leaf_block.v").write_text(
        "module leaf_block(input clk, input a, output y, output pass_clk);\n"
        "  reg r;\n"
        "  always @(posedge clk) r <= a;\n"
        "  assign y = r;\n"
        "  assign pass_clk = clk;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (src / "wrapper.v").write_text(
        "module top_wrap(input clk, input a, input we, input [2:0] addr, "
        "input [3:0] din, output [3:0] dout, output y);\n"
        "  wire mem_clk;\n"
        "  leaf_block u_leaf(.clk(clk), .a(a), .y(y), .pass_clk(mem_clk));\n"
        "  tiny_mem u_mem(.clk(mem_clk), .we(we), .addr(addr), .din(din), "
        ".dout(dout));\n"
        "endmodule\n",
        encoding="utf-8",
    )

    manifest_data = {
        "format": "autombist_instance_manifest",
        "schema_version": "1.0.0",
        "top_module": "top_wrap",
        "sources": {"wrapper": "wrapper.v"},
        "instances": [
            {
                "category": "memory",
                "hierarchical_path": "u_mem",
                "hierarchy_hint": "blackbox",
                "instance_name": "u_mem",
                "module_type": "tiny_mem",
                "sources": ["mem_stub.v"],
            },
            {
                "category": "instrument",
                "hierarchical_path": "u_leaf",
                "hierarchy_hint": "separate",
                "instance_name": "u_leaf",
                "module_type": "leaf_block",
                "sources": ["leaf_block.v"],
            },
        ],
    }
    manifest_path = src / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    manifest = load_autombist_manifest(manifest_path)
    # The guard (_check_composed_netlist_drivers, run inside
    # synthesize_from_manifest) already raises AssembleError on any driver
    # problem -- reaching the assertions below without an exception is itself
    # the core proof.
    result = synthesize_from_manifest(
        manifest,
        out=tmp_path / "out",
        liberty=SKY130_LIBERTY,
        cell_lib=SKY130_CELL_MAP,
    )

    composed = json.loads(result.composed_json_path.read_text(encoding="utf-8"))
    top = composed["modules"][result.top_module]
    cell_names = set(top["cells"].keys())
    assert "u_leaf" not in cell_names
    assert "u_mem" in cell_names

    # Synthesis aliases leaf_block's clk and pass_clk to one internal bit, so
    # the top-level clk port, u_mem's clock pin and every spliced flip-flop's
    # clock pin are ONE electrical net and must all carry the top-level clk
    # input's own bit -- the one member guaranteed to be driven. Comparing
    # them only to each other is not enough: the old flat remap sent both
    # the flip-flops and u_mem to the same UNDRIVEN mem_clk net, so they
    # still agreed with each other.
    top_clk = top["ports"]["clk"]["bits"][0]
    mem_clk_net = top["cells"]["u_mem"]["connections"]["clk"][0]
    ff_clock_nets = {
        cell["connections"]["CLK"][0]
        for name, cell in top["cells"].items()
        if name.startswith("u_leaf__") and "CLK" in cell.get("connections", {})
    }
    assert ff_clock_nets, "expected at least one spliced flip-flop"
    assert mem_clk_net == top_clk, (
        f"u_mem clock net {mem_clk_net} is not the top-level clk input "
        f"{top_clk} -- clk/pass_clk merge was lost"
    )
    assert ff_clock_nets == {top_clk}, (
        f"spliced flip-flop clock net(s) {ff_clock_nets} are not the "
        f"top-level clk input {top_clk} -- clk/pass_clk merge was lost"
    )
