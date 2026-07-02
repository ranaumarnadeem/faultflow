"""Tests for `faultflow.project.assemble`: composing a flat, single-top Yosys-JSON
SoC netlist out of a glue synthesis (blocks read via `-lib` so they survive as
blackbox instance cells) plus each block's own frozen, already-synthesized /
scanned / wrapped generic JSON.

See `faultflow/project/assemble.py` module docstring for the full algorithm.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.project.assemble import assemble_soc, block_stub_verilog, compose_soc

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.json"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _block_ports_fixture() -> dict[str, Any]:
    """Tiny block JSON with mixed 1-bit and multi-bit ports (for stub Verilog)."""
    return {
        "modules": {
            "block_x": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "a": {"direction": "input", "bits": [3, 4, 5, 6]},
                    "y": {"direction": "output", "bits": [7]},
                },
                "cells": {},
                "netnames": {},
            }
        }
    }


def _glue_json(inst_ports: dict[str, list[int]]) -> dict[str, Any]:
    """A tiny glue module `soc` with one real Sky130 cell and one instance cell
    `u_a` of type `block_a`, whose `connections` map is `inst_ports`."""
    return {
        "creator": "test",
        "modules": {
            "soc": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "top_in": {"direction": "input", "bits": [3]},
                    "top_out": {"direction": "output", "bits": [8]},
                },
                "cells": {
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__buf_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "X": "output"},
                        "connections": {"A": [3], "X": [8]},
                    },
                    "u_a": {
                        "hide_name": 0,
                        "type": "block_a",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            name: ("input" if name != "y" else "output")
                            for name in inst_ports
                        },
                        "connections": dict(inst_ports),
                    },
                },
                "netnames": {
                    "clk": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "top_in": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "top_out": {"hide_name": 0, "bits": [8], "attributes": {}},
                },
            },
            "block_a": {
                "attributes": {"blackbox": "1"},
                "ports": {
                    name: {
                        "direction": "input" if name != "y" else "output",
                        "bits": [0],
                    }
                    for name in inst_ports
                },
                "cells": {},
                "netnames": {},
            },
        },
    }


def _block_a_json(
    net_clk: int = 2, net_a: int = 3, net_b: int = 4, net_y: int = 5
) -> dict[str, Any]:
    """A tiny block_a generic JSON: two cells wired between clk/a/b -> y, using
    deliberately LOW net ids so they would collide with plausible glue ids if not
    remapped. One cell has a constant-tied input (A2=1'b1 style) to prove constants
    pass through unchanged."""
    internal = net_y + 1  # internal net between the two cells
    return {
        "creator": "test",
        "modules": {
            "block_a": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [net_clk]},
                    "a": {"direction": "input", "bits": [net_a]},
                    "b": {"direction": "input", "bits": [net_b]},
                    "y": {"direction": "output", "bits": [net_y]},
                },
                "cells": {
                    "c0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        "connections": {"A": [net_a], "B": [net_b], "X": [internal]},
                    },
                    "c1": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        # B tied to constant "1" -- must pass through unremapped.
                        "connections": {"A": [internal], "B": ["1"], "X": [net_y]},
                    },
                },
                "netnames": {
                    "clk": {"hide_name": 0, "bits": [net_clk], "attributes": {}},
                    "a": {"hide_name": 0, "bits": [net_a], "attributes": {}},
                    "b": {"hide_name": 0, "bits": [net_b], "attributes": {}},
                    "y": {"hide_name": 0, "bits": [net_y], "attributes": {}},
                    "internal_n": {
                        "hide_name": 0,
                        "bits": [internal],
                        "attributes": {},
                    },
                },
            }
        },
    }


def _block_a_with_wbc(
    net_clk: int = 2, net_a: int = 3, net_b: int = 4, net_y: int = 5
) -> dict[str, Any]:
    """block_a JSON with one extra `$wbc_in_scan_faultflow` cell wired like
    `faultflow/wrap/ports.py`'s `_scan_cell` (CLK/FROM_SYS/CTI/SE/TO_CORE/CTO,
    each a single-bit connection)."""
    data = _block_a_json(net_clk, net_a, net_b, net_y)
    module = data["modules"]["block_a"]
    max_bit = max(
        b
        for cell in module["cells"].values()
        for bits in cell["connections"].values()
        for b in bits
        if isinstance(b, int)
    )
    max_bit = max(max_bit, net_clk, net_a, net_b, net_y)
    cti = max_bit + 1
    se = max_bit + 2
    to_core = max_bit + 3
    cto = max_bit + 4
    module["cells"]["__wi_a"] = {
        "hide_name": 0,
        "type": "$wbc_in_scan_faultflow",
        "parameters": {},
        "attributes": {
            "faultflow_wbr": "input",
            "faultflow_wbr_chain": "0",
            "wbr_bit": "0",
        },
        "port_directions": {
            "CLK": "input",
            "FROM_SYS": "input",
            "CTI": "input",
            "SE": "input",
            "TO_CORE": "output",
            "CTO": "output",
        },
        "connections": {
            "CLK": [net_clk],
            "FROM_SYS": [net_a],
            "CTI": [cti],
            "SE": [se],
            "TO_CORE": [to_core],
            "CTO": [cto],
        },
    }
    module["netnames"]["__wi_a_cti"] = {
        "hide_name": 0,
        "bits": [cti],
        "attributes": {},
    }
    module["netnames"]["__wi_a_se"] = {
        "hide_name": 0,
        "bits": [se],
        "attributes": {},
    }
    module["netnames"]["__wi_a_to_core"] = {
        "hide_name": 0,
        "bits": [to_core],
        "attributes": {},
    }
    module["netnames"]["__wi_a_cto"] = {
        "hide_name": 0,
        "bits": [cto],
        "attributes": {},
    }
    return data


def _all_int_bits(module: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for port in module.get("ports", {}).values():
        out.update(b for b in port.get("bits", []) if isinstance(b, int))
    for net in module.get("netnames", {}).values():
        out.update(b for b in net.get("bits", []) if isinstance(b, int))
    for cell in module.get("cells", {}).values():
        for bits in cell.get("connections", {}).values():
            out.update(b for b in bits if isinstance(b, int))
    return out


# --------------------------------------------------------------------------- #
# 1. block_stub_verilog                                                       #
# --------------------------------------------------------------------------- #


def test_block_stub_verilog_declares_correct_ports() -> None:
    block_json = _block_ports_fixture()
    stub = block_stub_verilog(block_json, "block_x")

    assert "(* blackbox *)" in stub
    assert "module block_x" in stub
    assert "input clk" in stub
    assert "input [3:0] a" in stub
    assert "output y" in stub


# --------------------------------------------------------------------------- #
# 2. compose_soc splices block cells with remapped nets                       #
# --------------------------------------------------------------------------- #


def test_compose_soc_splices_block_cells_with_remapped_nets() -> None:
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    # Deliberately collide: block_a's internal net ids (2,3,4,5,6) overlap with
    # glue's own net ids (2=clk, 3=top_in/a, 8=top_out/y).
    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    glue_before_ids = _all_int_bits(glue["modules"]["soc"])

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )

    module = result["modules"]["soc"]
    cells = module["cells"]

    # (a) instance cell gone
    assert "u_a" not in cells

    # (b) block's cells present with u_a__ prefix
    assert "u_a__c0" in cells
    assert "u_a__c1" in cells

    # (c) + (d) net-id sanity: every net used by spliced cells is either one of
    # the glue-space port-boundary nets (2, 9, 8 for clk/b/y... note "a" -> glue
    # net 3 too) or a fresh id > any id used anywhere in the original glue.
    max_glue_id = max(glue_before_ids)
    spliced_nets: set[int] = set()
    for name in ("u_a__c0", "u_a__c1"):
        for bits in cells[name]["connections"].values():
            spliced_nets.update(b for b in bits if isinstance(b, int))

    port_boundary_nets = {2, 3, 9, 8}  # clk, a, b, y (glue-space)
    for net in spliced_nets:
        assert (
            net in port_boundary_nets or net > max_glue_id
        ), f"net {net} neither a port-boundary net nor freshly allocated"

    # No accidental collisions: glue's OWN cell (g0) nets vs spliced (non-port)
    # nets must not overlap.
    g0_nets = {
        b
        for bits in cells["g0"]["connections"].values()
        for b in bits
        if isinstance(b, int)
    }
    internal_spliced = spliced_nets - port_boundary_nets
    assert g0_nets.isdisjoint(internal_spliced)

    # (e) constant "1" on c1.B passed through unchanged.
    assert cells["u_a__c1"]["connections"]["B"] == ["1"]


# --------------------------------------------------------------------------- #
# 3. compose_soc tags WBC cells for aggregation                               #
# --------------------------------------------------------------------------- #


def test_compose_soc_tags_wbc_cells_for_aggregation(tmp_path: Path) -> None:
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    block_a = _block_a_with_wbc(net_clk=2, net_a=3, net_b=4, net_y=5)

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a},
        block_module={"u_a": "block_a"},
    )

    module = result["modules"]["soc"]
    cells = module["cells"]
    spliced_name = "u_a____wi_a"
    assert spliced_name in cells
    attrs = cells[spliced_name]["attributes"]
    assert attrs["faultflow_block"] == "u_a"
    assert isinstance(attrs["faultflow_block"], str)
    assert attrs["faultflow_wbc"] == "__wi_a"
    assert isinstance(attrs["faultflow_wbc"], str)

    netlist_path = tmp_path / "soc_composed.json"
    netlist_path.write_text(json.dumps(result), encoding="utf-8")

    from faultflow.project.aggregate import _wbc_pin_index

    index = _wbc_pin_index(netlist_path, "soc")
    # The remapped TO_CORE net of the spliced WBC-in cell must be indexed with
    # boundary_block == "u_a" and side == "in".
    to_core_net = cells[spliced_name]["connections"]["TO_CORE"][0]
    assert to_core_net in index
    boundary_block, boundary_wbc, pin, side = index[to_core_net]
    assert boundary_block == "u_a"
    assert boundary_wbc == "__wi_a"
    assert pin == "TO_CORE"
    assert side == "in"


# --------------------------------------------------------------------------- #
# 4. compose_soc: two blocks, no net collision                                #
# --------------------------------------------------------------------------- #


def test_compose_soc_two_blocks_no_net_collision() -> None:
    glue = _glue_json({"clk": [2], "a": [3], "b": [9], "y": [8]})
    module = glue["modules"]["soc"]
    # Add a second instance cell u_b of the same block type, wired to fully
    # separate glue-space nets so the assertion is a clean empty intersection.
    module["ports"]["b_in"] = {"direction": "input", "bits": [10]}
    module["ports"]["b_in2"] = {"direction": "input", "bits": [11]}
    module["ports"]["b_out"] = {"direction": "output", "bits": [12]}
    module["netnames"]["b_in"] = {"hide_name": 0, "bits": [10], "attributes": {}}
    module["netnames"]["b_in2"] = {"hide_name": 0, "bits": [11], "attributes": {}}
    module["netnames"]["b_out"] = {"hide_name": 0, "bits": [12], "attributes": {}}
    module["cells"]["u_b"] = {
        "hide_name": 0,
        "type": "block_a",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            "clk": "input",
            "a": "input",
            "b": "input",
            "y": "output",
        },
        # Fully separate glue-space nets from u_a (which uses 2, 3, 9, 8).
        "connections": {"clk": [2], "a": [10], "b": [11], "y": [12]},
    }
    glue["modules"]["block_a"] = {
        "attributes": {"blackbox": "1"},
        "ports": {
            "clk": {"direction": "input", "bits": [0]},
            "a": {"direction": "input", "bits": [0]},
            "b": {"direction": "input", "bits": [0]},
            "y": {"direction": "output", "bits": [0]},
        },
        "cells": {},
        "netnames": {},
    }

    # Same block type/JSON for both instances -- internal net-id spaces
    # originally overlap (both start at 2,3,4,5).
    block_a = _block_a_json(net_clk=2, net_a=3, net_b=4, net_y=5)

    result = compose_soc(
        glue_json=glue,
        soc_top="soc",
        blocks={"u_a": block_a, "u_b": block_a},
        block_module={"u_a": "block_a", "u_b": "block_a"},
    )

    cells = result["modules"]["soc"]["cells"]
    assert "u_a" not in cells
    assert "u_b" not in cells

    a_nets: set[int] = set()
    b_nets: set[int] = set()
    for name, cell in cells.items():
        if name.startswith("u_a__"):
            a_nets.update(
                b
                for bits in cell["connections"].values()
                for b in bits
                if isinstance(b, int)
            )
        elif name.startswith("u_b__"):
            b_nets.update(
                b
                for bits in cell["connections"].values()
                for b in bits
                if isinstance(b, int)
            )

    assert a_nets, "expected u_a__ prefixed cells with net connections"
    assert b_nets, "expected u_b__ prefixed cells with net connections"
    assert a_nets.isdisjoint(b_nets)


# --------------------------------------------------------------------------- #
# 5. assemble_soc end-to-end                                                  #
# --------------------------------------------------------------------------- #


def test_assemble_soc_end_to_end(tmp_path: Path, require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    from faultflow.runner.runner import _load_core
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge
    from faultflow.wrap.ports import wrap_ports

    core = _load_core()
    assert core is not None

    def _build_wrapped_block(name: str, op: str) -> Path:
        rtl = tmp_path / f"{name}.v"
        rtl.write_text(
            f"module {name}(input clk, input a, input b, output y);\n"
            f"  reg r;\n"
            f"  always @(posedge clk) r <= a {op} b;\n"
            f"  assign y = ~r;\n"
            f"endmodule\n",
            encoding="utf-8",
        )
        session = ProjectSession(output_root=tmp_path / "out" / name)
        bridge = TclBridge(session)
        bridge.call("read_netlist", str(rtl), "-top", name)
        bridge.call("use_lib_cells", "sky130")
        bridge.call("add_clock", "clk")
        bridge.call("synth")
        bridge.call("add_scan", "-chains", "1")

        cfg = session.materialize_config()
        scan_json = cfg.intermediate_dir / f"{name}_scan.json"
        assert scan_json.exists(), f"scan-stitched JSON not found at {scan_json}"
        scanned = json.loads(scan_json.read_text(encoding="utf-8"))
        wrapped = wrap_ports(scanned, name, wbr_model="scan")

        out_path = tmp_path / f"{name}_wrapped.json"
        out_path.write_text(json.dumps(wrapped), encoding="utf-8")
        return out_path

    block_a_path = _build_wrapped_block("block_a", "&")
    block_b_path = _build_wrapped_block("block_b", "|")

    soc_rtl = tmp_path / "soc.v"
    soc_rtl.write_text(
        "module soc(input clk, input a, input b, output y);\n"
        "  wire w;\n"
        "  wire a_scan_out, a_wbr_so, b_scan_out, b_wbr_so;\n"
        "  block_a u_a(\n"
        "    .clk(clk), .a(a), .b(b), .y(w),\n"
        "    .scan_en(1'b0), .scan_in(1'b0), .scan_out(a_scan_out),\n"
        "    .CLK(clk), .wbr_se(1'b0), .wbr_si(1'b0), .wbr_so(a_wbr_so)\n"
        "  );\n"
        "  block_b u_b(\n"
        "    .clk(clk), .a(w), .b(b), .y(y),\n"
        "    .scan_en(1'b0), .scan_in(1'b0), .scan_out(b_scan_out),\n"
        "    .CLK(clk), .wbr_se(1'b0), .wbr_si(1'b0), .wbr_so(b_wbr_so)\n"
        "  );\n"
        "endmodule\n",
        encoding="utf-8",
    )

    liberty = ROOT / "cells" / "sky130" / "sky130_fd_sc_hd.lib"
    if not liberty.exists():
        candidates = list((ROOT / "cells" / "sky130").glob("*.lib"))
        assert candidates, "no sky130 liberty file found for assemble_soc"
        liberty = candidates[0]

    output_json = tmp_path / "soc_composed.json"
    workdir = tmp_path / "assemble_work"

    result_path = assemble_soc(
        soc_rtl=soc_rtl,
        soc_top="soc",
        liberty=liberty,
        blocks={"u_a": block_a_path, "u_b": block_b_path},
        block_module={"u_a": "block_a", "u_b": "block_b"},
        output_json=output_json,
        workdir=workdir,
    )
    assert result_path == output_json
    assert output_json.exists()

    composed = json.loads(output_json.read_text(encoding="utf-8"))
    assert list(composed["modules"].keys()) == ["soc"]

    vectors = [
        {"clk": False, "a": False, "b": False},
        {"clk": False, "a": True, "b": True},
        {"clk": False, "a": True, "b": False},
    ]
    outputs = core.fault_free_outputs(
        str(output_json),
        str(CELL_MAP),
        vectors,
        ["clk", "a", "b"],
        ["y"],
    )
    assert len(outputs) == len(vectors)
