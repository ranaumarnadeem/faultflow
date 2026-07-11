"""Unit tests for faultflow.wrap.ports (IEEE 1500 wrapper insertion)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.wrap import WrapError, wrap_ports

_REPO = Path(__file__).resolve().parents[3]
_CELL_MAP = _REPO / "cells" / "sky130" / "sky130_fd_sc_hd.json"


def _and_core() -> dict[str, Any]:
    """Tiny combinational core: y = a & b (Yosys-JSON shape)."""
    return {
        "creator": "test",
        "modules": {
            "core": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "b": {"direction": "input", "bits": [3]},
                    "y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "g0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__and2_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "B": "input", "X": "output"},
                        "connections": {"A": [2], "B": [3], "X": [4]},
                    }
                },
                "netnames": {
                    "a": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "b": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "y": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        },
    }


def _clocked_core() -> dict[str, Any]:
    """Tiny sequential core: a single D flip-flop (clk, d -> q)."""
    return {
        "creator": "test",
        "modules": {
            "core": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "d": {"direction": "input", "bits": [3]},
                    "q": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "Q": [4]},
                    }
                },
                "netnames": {
                    "clk": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "d": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        },
    }


def _cells(netlist: dict[str, Any]) -> dict[str, Any]:
    return netlist["modules"]["core"]["cells"]


@pytest.mark.parametrize("model", ["buffer", "scan"])
def test_clock_is_never_wrapped(model: str) -> None:
    """The clock is a functional control port, not a WBR data boundary -- neither
    model may wrap it. The buffer model used to (its clock-exclusion was gated on
    the scan model), rewiring the core FF's CLK to an internal net so scan-check
    could no longer map it to an input port -- breaking the whole buffer flow."""
    out = wrap_ports(_clocked_core(), wbr_model=model, clock="clk")
    mod = out["modules"]["core"]
    cells = mod["cells"]

    # No wrapper cell inserted for the clock port.
    assert "__wi_clk" not in cells
    # The core FF's CLK pin still reads the original clk input net (net 2),
    # NOT an internal wrapper output net.
    assert cells["u0"]["connections"]["CLK"] == [2]
    # The clk input port survives, still on net 2.
    assert mod["ports"]["clk"] == {"direction": "input", "bits": [2]}
    # The data port d IS wrapped in both models.
    assert "__wi_d" in cells


def test_buffer_model_inserts_transparent_wrappers() -> None:
    out = wrap_ports(_and_core(), wbr_model="buffer")
    cells = _cells(out)
    assert cells["__wi_a"]["type"] == "$wbc_in_faultflow"
    assert cells["__wi_b"]["type"] == "$wbc_in_faultflow"
    assert cells["__wo_y"]["type"] == "$wbc_out_faultflow"
    # Output port redirected to the new system-side net (no longer net 4).
    assert out["modules"]["core"]["ports"]["y"]["bits"] != [4]
    # No scan ports added in buffer mode.
    assert "wbr_si" not in out["modules"]["core"]["ports"]


def test_scan_model_emits_scan_cells_with_chain_tags() -> None:
    out = wrap_ports(_and_core(), wbr_model="scan")
    mod = out["modules"]["core"]
    cells = mod["cells"]
    assert cells["__wi_a"]["type"] == "$wbc_in_scan_faultflow"
    assert cells["__wo_y"]["type"] == "$wbc_out_scan_faultflow"
    # Every scan wrapper is tagged with side + chain + bit position.
    for name in ("__wi_a", "__wi_b", "__wo_y"):
        attrs = cells[name]["attributes"]
        assert attrs["faultflow_wbr"] in {"input", "output"}
        assert attrs["faultflow_wbr_chain"] == "0"
    bits = {
        n: int(cells[n]["attributes"]["wbr_bit"])
        for n in ("__wi_a", "__wi_b", "__wo_y")
    }
    assert sorted(bits.values()) == [0, 1, 2]
    # Wrapper scan ports exist.
    assert mod["ports"]["wbr_si"]["direction"] == "input"
    assert mod["ports"]["wbr_so"]["direction"] == "output"
    assert mod["ports"]["wbr_se"]["direction"] == "input"


def test_scan_chain_is_a_daisy_si_to_so() -> None:
    out = wrap_ports(_and_core(), wbr_model="scan")
    mod = out["modules"]["core"]
    cells = mod["cells"]
    si = mod["ports"]["wbr_si"]["bits"][0]
    so = mod["ports"]["wbr_so"]["bits"][0]
    order = sorted(
        ("__wi_a", "__wi_b", "__wo_y"),
        key=lambda n: int(cells[n]["attributes"]["wbr_bit"]),
    )
    # Head reads scan-in; each cell's CTO feeds the next cell's CTI; tail == scan-out.
    prev = si
    for name in order:
        assert cells[name]["connections"]["CTI"][0] == prev
        prev = cells[name]["connections"]["CTO"][0]
    assert prev == so
    # Shared clock + scan-enable across the whole chain.
    clk = {cells[n]["connections"]["CLK"][0] for n in order}
    se = {cells[n]["connections"]["SE"][0] for n in order}
    assert len(clk) == 1 and len(se) == 1


def test_targets_subset_only_wraps_named_ports() -> None:
    out = wrap_ports(_and_core(), wbr_model="buffer", targets=["a"])
    cells = _cells(out)
    assert "__wi_a" in cells
    assert "__wi_b" not in cells
    assert "__wo_y" not in cells


def test_unknown_target_raises() -> None:
    with pytest.raises(WrapError):
        wrap_ports(_and_core(), wbr_model="buffer", targets=["nope"])


def test_bad_model_raises() -> None:
    with pytest.raises(WrapError):
        wrap_ports(_and_core(), wbr_model="nonsense")


def test_scan_wrapped_netlist_drives_core_in_intest(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """End-to-end: wrap y=a&b with scan WBR, then deliver the core's inputs from
    the loaded boundary register (INTEST) and read y back through the unload."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None

    out = wrap_ports(_and_core(), wbr_model="scan")
    netlist = tmp_path / "core_scan.json"
    netlist.write_text(json.dumps(out), encoding="utf-8")
    cells = out["modules"]["core"]["cells"]
    order = sorted(
        ("__wi_a", "__wi_b", "__wo_y"),
        key=lambda n: int(cells[n]["attributes"]["wbr_bit"]),
    )
    pos = {n: i for i, n in enumerate(order)}  # __wi_a=0, __wi_b=1, __wo_y=2

    def run(qa: bool, qb: bool) -> bool:
        # Descending-position load: offset i ends at chain position (len-1-i).
        load = [False, False, False]
        load[len(order) - 1 - pos["__wi_a"]] = qa
        load[len(order) - 1 - pos["__wi_b"]] = qb
        res = core.simulate_scan_pattern(
            json_path=str(netlist),
            cell_map_path=str(_CELL_MAP),
            clock_ports=["CLK"],
            clock_off_states=[False],
            scan_enable_port="wbr_se",
            scan_input_ports=["wbr_si"],
            scan_output_ports=["wbr_so"],
            functional_output_ports=[],
            max_chain_length=len(order),
            load_seqs={0: load},
            capture_pi_values={"a": False, "b": False},
            unsupported_policy="blackbox",
            test_mode="intest",
        )
        return bool(res["unload_seqs"][0][0])  # first unload bit = __wo_y.q

    # y = qa & qb, delivered to the core from the loaded boundary register.
    assert run(False, False) is False
    assert run(True, False) is False
    assert run(False, True) is False
    assert run(True, True) is True
