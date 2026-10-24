"""Where the scan ATPG view grades each fault on a scan flop's pins
(faultflow.scan.site_resolution.build_scan_execution_map).

The view replaces each scan flop by pseudo-ports, so a fault on a net the flop
drives or reads must be graded at the view net that carries the same logic.
A branch reaches one reader: grading it anywhere shared with other readers or
a primary output credits detections the branch fault can't make.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from scan_credit import credit_not_reproduced

from faultflow.runner.runner import _load_core
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.site_resolution import (
    build_scan_execution_map,
    build_site_key_index,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = str(ROOT / "cells/sky130/sky130_fd_sc_hd.json")
TOP = "qmap"
CLK, SE, SDI, RST, A, B = 2, 3, 4, 5, 6, 7
PLAIN = "$scanff_faultflow"
RESET = "$scanff_r_faultflow"
SET = "$scanff_s_faultflow"


def _gate(cell_type: str, **pins: int) -> dict[str, Any]:
    return {"type": cell_type, "connections": {p: [b] for p, b in pins.items()}}


def _flop(kind: str, d: int, q: int) -> dict[str, Any]:
    pins = {"CLK": [CLK], "D": [d], "SE": [SE], "Q": [q]}
    if kind == RESET:
        pins["RESET_B"] = [RST]
    elif kind == SET:
        pins["SET_B"] = [RST]
    return {"type": kind, "connections": pins}


def _design(
    flops: list[tuple[str, dict[str, Any]]],
    gates: dict[str, dict[str, Any]],
    outputs: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A scanned netlist: `flops` (instance, cell) in chain order -- each
    flop's SDI is set to the previous one's Q, the first's to sdi, and the
    last's Q is the scan output -- plus combinational `gates` and primary
    `outputs` (name -> net)."""
    cells: dict[str, Any] = dict(gates)
    records = []
    previous = SDI
    for position, (instance, cell) in enumerate(flops):
        cell["connections"]["SDI"] = [previous]
        cells[instance] = cell
        records.append(
            {
                "instance": instance,
                "chain_index": 0,
                "chain_position": position,
                "q_net": cell["connections"]["Q"][0],
                "data_net": cell["connections"]["D"][0],
                "clock_net": CLK,
            }
        )
        previous = cell["connections"]["Q"][0]
    ports: dict[str, Any] = {
        "clk": {"direction": "input", "bits": [CLK]},
        "se": {"direction": "input", "bits": [SE]},
        "sdi": {"direction": "input", "bits": [SDI]},
        "rst_n": {"direction": "input", "bits": [RST]},
        "a": {"direction": "input", "bits": [A]},
        "b": {"direction": "input", "bits": [B]},
        "sdo": {"direction": "output", "bits": [previous]},
    }
    for name, net in outputs.items():
        ports[name] = {"direction": "output", "bits": [net]}
    generic = {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": {n: {"bits": p["bits"]} for n, p in ports.items()},
            }
        }
    }
    manifest = {
        "top": TOP,
        "clock_net": CLK,
        "scan_enable": "se",
        "scan_inputs": ["sdi"],
        "scan_outputs": ["sdo"],
        "chains": [{"index": 0, "length": len(flops)}],
        "max_chain_length": len(flops),
        "cells": records,
    }
    return generic, manifest


class _Mapped:
    def __init__(
        self, tmp_path: Path, generic: dict[str, Any], manifest: dict[str, Any]
    ):
        core = _load_core()
        assert core is not None
        view, self.pseudo = build_scan_atpg_view(generic, manifest)
        self.view = view["modules"][TOP]
        generic_path = tmp_path / "generic.json"
        view_path = tmp_path / "view.json"
        generic_path.write_text(json.dumps(generic), encoding="utf-8")
        view_path.write_text(json.dumps(view), encoding="utf-8")
        self.generic_sites = build_site_key_index(core, generic_path, CELL_MAP, "fail")
        self.view_sites = build_site_key_index(core, view_path, CELL_MAP, "fail")
        self.execution, self.exclusions = build_scan_execution_map(
            core,
            generic_path,
            view_path,
            CELL_MAP,
            CELL_MAP,
            "fail",
            self.pseudo,
            manifest,
        )

    def q_drive(self, instance: str) -> int:
        return int(self.pseudo[instance]["q_drive_net_id"])

    def graded_at(self, generic_key: str) -> int:
        assert generic_key in self.generic_sites, generic_key
        return self.execution[generic_key]

    def view_net_driven_by(self, cell_name: str) -> int:
        cell = self.view["cells"][cell_name]
        (net,) = [b for b in cell["connections"]["Y"]]
        return int(net)


@pytest.mark.unit
@pytest.mark.parametrize("kind", [RESET, SET])
def test_each_q_branch_of_an_async_flop_is_graded_at_its_own_reader(
    tmp_path: Path, require_cpp_core: None, kind: str
) -> None:
    """ff0's Q (net 10) feeds two gates and ff1's SDI. In the view the gates
    read the async-control mux's output, not the PPI."""
    generic, manifest = _design(
        [("ff0", _flop(kind, A, 10)), ("ff1", _flop(kind, 11, 12))],
        {
            "g_and": _gate("sky130_fd_sc_hd__and2_1", A=10, B=B, X=11),
            "g_or": _gate("sky130_fd_sc_hd__or2_1", A=10, B=A, X=13),
        },
        {"y": 13},
    )
    m = _Mapped(tmp_path, generic, manifest)

    q_drive = m.q_drive("ff0")
    assert q_drive != m.pseudo["ff0"]["ppi_net_id"]
    for reader, pin in (("g_and", "A"), ("g_or", "A")):
        assert (
            m.graded_at(f"net:10:branch:{reader}:{pin}")
            == m.view_sites[f"net:{q_drive}:branch:{reader}:{pin}"]
        )
    # The SDI branch is scan-path only.
    assert m.exclusions["net:10:branch:ff1:SDI"] == "scan_internal"


@pytest.mark.unit
@pytest.mark.parametrize("kind", [PLAIN, RESET])
def test_a_one_reader_q_that_is_also_an_output_keeps_its_branch(
    tmp_path: Path, require_cpp_core: None, kind: str
) -> None:
    """ff0's Q (net 10) feeds one gate, ff1's SDI and the output q_po. In the
    view the SDI edge is gone; without a buffer on the output branch, the
    gate's branch fault would be graded at a net q_po also observes."""
    generic, manifest = _design(
        [("ff0", _flop(kind, A, 10)), ("ff1", _flop(kind, B, 12))],
        {"g_inv": _gate("sky130_fd_sc_hd__inv_1", A=10, Y=11)},
        {"y": 11, "q_po": 10},
    )
    m = _Mapped(tmp_path, generic, manifest)

    q_drive = m.q_drive("ff0")
    assert (
        m.graded_at("net:10:branch:g_inv:A")
        == m.view_sites[f"net:{q_drive}:branch:g_inv:A"]
    )
    # q_po reads its own branch of q_drive.
    (po_bit,) = m.view["ports"]["q_po"]["bits"]
    assert po_bit != q_drive
    assert f"net:{q_drive}:stem" in m.view_sites


@pytest.mark.unit
@pytest.mark.parametrize(("driver", "reader"), [("u_a", "u_b"), ("u_b", "u_a")])
def test_a_flop_to_flop_branch_is_graded_at_the_reading_flops_capture(
    tmp_path: Path, require_cpp_core: None, driver: str, reader: str
) -> None:
    """The driver's Q (net 10) feeds the reader flop's D and a gate. Both
    instance orders: the view processes flops sorted by name, and a flop
    processed later reads a D pin an earlier rewire already changed."""
    generic, manifest = _design(
        [(driver, _flop(RESET, A, 10)), (reader, _flop(RESET, 10, 12))],
        {"g": _gate("sky130_fd_sc_hd__inv_1", A=10, Y=11)},
        {"y": 11},
    )
    m = _Mapped(tmp_path, generic, manifest)

    assert (
        m.graded_at(f"net:10:branch:{reader}:D")
        == m.view_sites[f"net:{m.view_net_driven_by(f'$ffbranch_{reader}')}:stem"]
    )
    assert (
        m.graded_at("net:10:branch:g:A")
        == m.view_sites[f"net:{m.q_drive(driver)}:branch:g:A"]
    )


@pytest.mark.unit
def test_a_self_loop_branch_is_graded_at_the_flops_capture(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A hold flop: its own Q (net 10) is its D, and a gate reads it too."""
    generic, manifest = _design(
        [("ff0", _flop(RESET, 10, 10))],
        {"g": _gate("sky130_fd_sc_hd__inv_1", A=10, Y=11)},
        {"y": 11},
    )
    m = _Mapped(tmp_path, generic, manifest)

    assert (
        m.graded_at("net:10:branch:ff0:D")
        == m.view_sites[f"net:{m.view_net_driven_by('$ffbranch_ff0')}:stem"]
    )


@pytest.mark.unit
@pytest.mark.parametrize("sync_name", ["a_sync", "z_sync"])
def test_a_reset_driven_by_a_scan_flop_is_graded_at_each_flops_control(
    tmp_path: Path, require_cpp_core: None, sync_name: str
) -> None:
    """A reset synchronizer: its Q (net 10) is both flops' RESET_B. Named to
    be processed before and after them."""
    sync = _flop(PLAIN, A, 10)
    ff_x = _flop(RESET, B, 12)
    ff_y = _flop(RESET, A, 13)
    ff_x["connections"]["RESET_B"] = [10]
    ff_y["connections"]["RESET_B"] = [10]
    generic, manifest = _design(
        [(sync_name, sync), ("ff_x", ff_x), ("ff_y", ff_y)], {}, {}
    )
    m = _Mapped(tmp_path, generic, manifest)

    for flop in ("ff_x", "ff_y"):
        assert (
            m.graded_at(f"net:10:branch:{flop}:RESET_B")
            == m.view_sites[f"net:{m.view_net_driven_by(f'$ffctrlbranch_{flop}')}:stem"]
        )


# q0 (async reset) feeds two gates; q1 (async reset) and p0 (no reset) each
# feed one gate and an output.
QFAN_RTL = """\
module qfan (input clk, input rst_n, input a, input b, input c,
             output y0, output y1, output q_po, output p_po, output y2);
  reg q0, q1, q2, q3, p0;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      q0 <= 1'b0; q1 <= 1'b0; q2 <= 1'b0; q3 <= 1'b0;
    end else begin
      q0 <= a ^ b;
      q1 <= q0 & c;
      q2 <= q0 | b;
      q3 <= ~q1;
    end
  always @(posedge clk) p0 <= a & c;
  assign y0 = q2 ^ q3;
  assign y1 = q3;
  assign q_po = q1;
  assign p_po = p0;
  assign y2 = ~p0;
endmodule
"""


@pytest.mark.integration
def test_every_credited_fault_reproduces_in_the_scan_protocol(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The reduced view is a shortcut; every detection it credits must be one
    the full protocol makes with the same patterns (CLAUDE.md #24)."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    rtl = tmp_path / "qfan.v"
    rtl.write_text(QFAN_RTL, encoding="utf-8")
    patterns = tmp_path / "patterns.json"
    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "qfan")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    bridge.call("add_scan", "-chains", "1")
    bridge.call("check_scan")
    bridge.call("run_atpg", "-scan", "-export-patterns", str(patterns))

    assert credit_not_reproduced(session.materialize_config(), patterns) == []
