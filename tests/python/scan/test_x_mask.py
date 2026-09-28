"""A blackbox output's value is unknown during a scan test (faultflow.scan.x_mask):
every observation point it reaches is masked, for the campaign's launch mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.runner.runner import RunnerError, _load_core
from faultflow.scan import ScanError
from faultflow.scan.atpg_view import (
    UNOBSERVED_NETS_ATTR,
    build_scan_atpg_view,
    make_blackbox_transparent,
)
from faultflow.scan.pattern_export import scan_pattern_from_dict, scan_pattern_to_dict
from faultflow.scan.protocol import ScanPattern, serialize_vector
from faultflow.scan.verify import verify_golden_scan_protocol
from faultflow.scan.x_mask import (
    XMask,
    apply_x_mask,
    check_x_off_scan_path,
    compute_x_mask,
    recorded_x_mask,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = str(ROOT / "cells/sky130/sky130_fd_sc_hd.json")
TOP = "xm"
CLK, SE, SDI, PI = 2, 3, 4, 5
Q = (100, 101, 102)
DOUT = (200, 201)
PO = 300


def _design(
    ff0_type: str = "$scanff_faultflow", **overrides: list[int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A scan chain ff0 -> ff1 -> ff2 and a memory u_mem: ff0 captures
    dout[0], ff1 captures ff0's state, ff2 the primary input pi, and the
    primary output po = dout[1] & pi. ff0 is a `ff0_type`, and `overrides`
    rewires its pins."""
    data = (DOUT[0], Q[0], PI)
    cells: dict[str, Any] = {
        "u_mem": {
            "type": "sram_like",
            "port_directions": {"din": "input", "dout": "output"},
            "connections": {"din": [PI], "dout": list(DOUT)},
        },
        "g_po": {
            "type": "sky130_fd_sc_hd__and2_1",
            "port_directions": {"A": "input", "B": "input", "X": "output"},
            "connections": {"A": [DOUT[1]], "B": [PI], "X": [PO]},
        },
    }
    records = []
    for i in range(3):
        cells[f"ff{i}"] = {
            "type": "$scanff_faultflow",
            "connections": {
                "CLK": [CLK],
                "D": [data[i]],
                "SDI": [SDI if i == 0 else Q[i - 1]],
                "SE": [SE],
                "Q": [Q[i]],
            },
        }
        records.append(
            {
                "instance": f"ff{i}",
                "chain_index": 0,
                "chain_position": i,
                "q_net": Q[i],
                "data_net": data[i],
                "clock_net": CLK,
            }
        )
    cells["ff0"]["type"] = ff0_type
    cells["ff0"]["connections"].update(overrides)
    ports = {
        "clk": {"direction": "input", "bits": [CLK]},
        "se": {"direction": "input", "bits": [SE]},
        "sdi": {"direction": "input", "bits": [SDI]},
        "pi": {"direction": "input", "bits": [PI]},
        "sdo": {"direction": "output", "bits": [Q[2]]},
        "po": {"direction": "output", "bits": [PO]},
    }
    netnames = {name: {"bits": port["bits"]} for name, port in ports.items()}
    generic = {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": netnames,
            }
        }
    }
    manifest = {
        "top": TOP,
        "clock_net": CLK,
        "scan_enable": "se",
        "scan_inputs": ["sdi"],
        "scan_outputs": ["sdo"],
        "chains": [{"index": 0, "length": 3}],
        "max_chain_length": 3,
        "cells": records,
    }
    return generic, manifest


def _view_file(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    generic, manifest = _design()
    view, _ = build_scan_atpg_view(generic, manifest, blackbox_instances=["u_mem"])
    path = tmp_path / "view.json"
    path.write_text(json.dumps(view), encoding="utf-8")
    return view, path


def _mask(path: Path, launch_mode: str | None, x_instances=("u_mem",)) -> XMask:
    return compute_x_mask(
        _load_core(),
        path,
        top=TOP,
        cell_map=CELL_MAP,
        unsupported="fail",
        x_instances=x_instances,
        launch_mode=launch_mode,
        functional_output_order=["po"],
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("launch_mode", "ppo_ports", "launch_ppo_ports"),
    [
        # dout[0] reaches only ff0's capture.
        (None, {"__ppo_ff0"}, set()),
        # A launch-on-shift capture frame is loaded by shifting: no flop
        # carries a captured unknown into it.
        ("los", {"__ppo_ff0"}, set()),
        # Launch-on-capture: ff0 captures the unknown at launch and holds it
        # through the capture frame, where ff1 captures it.
        ("loc", {"__ppo_ff0", "__ppo_ff1"}, {"__ppo_ff0"}),
    ],
)
def test_mask_is_the_cone_of_the_unknown_outputs(
    tmp_path: Path,
    require_cpp_core: None,
    launch_mode: str | None,
    ppo_ports: set,
    launch_ppo_ports: set,
) -> None:
    view, path = _view_file(tmp_path)
    mask = _mask(path, launch_mode)

    ports = view["modules"][TOP]["ports"]
    assert mask.launch_mode == launch_mode
    assert mask.ppo_ports == ppo_ports
    assert mask.launch_ppo_ports == launch_ppo_ports
    assert mask.outputs == {"po"}
    assert mask.nets == {ports[p]["bits"][0] for p in (*ppo_ports, "po")}


@pytest.mark.unit
def test_no_transition_is_credited_at_a_flop_launched_unknown() -> None:
    """A flop that captures an unknown value at the LOC launch edge has an
    unknown state after it, so a transition at its Q is unknown too: none of
    its Q-stem faults may be credited, whatever the protocol sim sees."""
    from faultflow.scan.detection_pipeline import (
        ScanPipelineContext,
        _FaultRow,
        _launch_known,
    )

    ctx = ScanPipelineContext(
        cfg=None,  # type: ignore[arg-type]
        manifest={},
        generic_json=Path("generic.json"),
        pseudo_port_map={},
        functional_output_order=[],
        q_stem_site_keys=frozenset({"net:100:stem", "net:101:stem"}),
        q_stem_to_ppo={"net:100:stem": "__ppo_ff0", "net:101:stem": "__ppo_ff1"},
        x_mask=XMask(
            launch_mode="loc",
            ppo_ports=frozenset({"__ppo_ff0", "__ppo_ff1"}),
            launch_ppo_ports=frozenset({"__ppo_ff0"}),
        ),
    )

    def row(site: str) -> _FaultRow:
        return _FaultRow(fault_id=1, fault_site_key=site, fault_type="sa0")

    assert not _launch_known(ctx, row("net:100:stem"))
    # ff1 captures the unknown only at the capture edge: its launch is known.
    assert _launch_known(ctx, row("net:101:stem"))
    assert _launch_known(ctx, row("net:5:stem"))


@pytest.mark.unit
def test_an_output_asserted_known_masks_nothing(
    tmp_path: Path, require_cpp_core: None
) -> None:
    _, path = _view_file(tmp_path)
    assert _mask(path, None, x_instances=()) == XMask()


@pytest.mark.unit
def test_the_core_honours_a_recorded_mask(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Recorded in the view, the mask leaves the masked points out of every
    observable set: from the core's view, dout now reaches nothing."""
    view, path = _view_file(tmp_path)
    mask = _mask(path, None)
    apply_x_mask(view, TOP, mask)
    assert recorded_x_mask(view, TOP) == mask.nets
    masked = tmp_path / "masked.json"
    masked.write_text(json.dumps(view), encoding="utf-8")

    core = _load_core()
    assert core is not None
    reach = core.combinational_reach(str(masked), CELL_MAP, list(DOUT))
    assert reach["observable"] == []

    # The twin observes them again.
    assert make_blackbox_transparent(view, TOP)
    assert UNOBSERVED_NETS_ATTR not in view["modules"][TOP]["attributes"]


@pytest.mark.unit
def test_an_empty_mask_is_still_recorded() -> None:
    generic, _ = _design()
    apply_x_mask(generic, TOP, XMask())
    assert recorded_x_mask(generic, TOP) == frozenset()


def _check(tmp_path: Path, generic: dict[str, Any], manifest: dict[str, Any]) -> None:
    path = tmp_path / "generic.json"
    path.write_text(json.dumps(generic), encoding="utf-8")
    check_x_off_scan_path(
        _load_core(),
        path,
        manifest,
        cell_map=CELL_MAP,
        unsupported="fail",
        blackbox_instances=["u_mem"],
        x_instances=["u_mem"],
    )


@pytest.mark.unit
def test_an_unknown_output_may_reach_a_flop_capture(
    tmp_path: Path, require_cpp_core: None
) -> None:
    _check(tmp_path, *_design())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ff0_type", "pin", "role"),
    [
        ("$scanff_faultflow", "CLK", "clock"),
        ("$scanff_faultflow", "SE", "scan_enable"),
        # An async clear or preset forces the flop during shift too.
        ("$scanff_r_faultflow", "RESET_B", "clear"),
        ("$scanff_s_faultflow", "SET_B", "preset"),
    ],
)
def test_an_unknown_output_on_the_scan_path_is_an_error(
    tmp_path: Path, require_cpp_core: None, ff0_type: str, pin: str, role: str
) -> None:
    with pytest.raises(ScanError, match=rf"ff0\.{role}\b"):
        _check(tmp_path, *_design(ff0_type, **{pin: [DOUT[1]]}))


@pytest.mark.unit
def test_combinational_reach_names_a_clear_apart_from_an_enable(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A flop's third input slot holds a clear or an enable. Only the clear
    is on the scan path, so the reach must say which one it arrived at."""
    src, q_en, q_clr = 10, 20, 21
    cells = {
        "u_en": {
            "type": "sky130_fd_sc_hd__edfxtp_1",
            "connections": {"CLK": [CLK], "D": [PI], "DE": [src], "Q": [q_en]},
        },
        "u_clr": {
            "type": "$scanff_r_faultflow",
            "connections": {
                "CLK": [CLK],
                "D": [PI],
                "SDI": [SDI],
                "SE": [SE],
                "RESET_B": [src],
                "Q": [q_clr],
            },
        },
    }
    ports = {
        "clk": {"direction": "input", "bits": [CLK]},
        "se": {"direction": "input", "bits": [SE]},
        "sdi": {"direction": "input", "bits": [SDI]},
        "pi": {"direction": "input", "bits": [PI]},
        "src": {"direction": "input", "bits": [src]},
        "q_en": {"direction": "output", "bits": [q_en]},
        "q_clr": {"direction": "output", "bits": [q_clr]},
    }
    path = tmp_path / "reach.json"
    path.write_text(
        json.dumps(
            {
                "modules": {
                    TOP: {
                        "attributes": {"top": "1"},
                        "ports": ports,
                        "cells": cells,
                        "netnames": {n: {"bits": p["bits"]} for n, p in ports.items()},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    core = _load_core()
    assert core is not None
    found = core.combinational_reach(str(path), CELL_MAP, [src])
    assert sorted(found["flop_inputs"]) == [(q_en, "enable"), (q_clr, "clear")]


def _pseudo_port_map() -> dict[str, dict[str, Any]]:
    return {
        f"ff{i}": {
            "ppi_port": f"__ppi_ff{i}",
            "ppo_port": f"__ppo_ff{i}",
            "chain_id": 0,
            "position_in_chain": i,
        }
        for i in range(3)
    }


@pytest.mark.unit
def test_serialize_vector_marks_masked_bits_dont_care() -> None:
    """Unload shifts the highest chain position out first, and a chain shorter
    than max_chain_length pads after its bits -- zeros shifted in, known."""
    manifest = {"chains": [{"index": 0, "length": 3}], "max_chain_length": 4}
    vector = {"__ppo_ff0": True, "__ppo_ff1": False, "__ppo_ff2": True, "po": True}

    pattern = serialize_vector(
        vector,
        _pseudo_port_map(),
        manifest,
        masked_ppo_ports=frozenset({"__ppo_ff0"}),
        masked_outputs=frozenset({"po"}),
    )

    assert pattern.expected_unload == {0: [True, False, True, False]}
    assert pattern.unload_mask == {0: [True, True, False, True]}
    assert "po" not in pattern.capture_pi_values
    unmasked = serialize_vector(vector, _pseudo_port_map(), manifest)
    assert unmasked.unload_mask is None
    assert unmasked.capture_pi_values["po"] is True


class _FakeCore:
    def __init__(self, unload: list[bool]) -> None:
        self.unload = unload

    def simulate_scan_pattern(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"unload_seqs": {0: self.unload}, "real_po_values": {}}


@pytest.mark.unit
def test_golden_gate_skips_dont_care_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faultflow.runner.runner as runner_mod

    generic, manifest = _design()
    netlist = tmp_path / "generic.json"
    netlist.write_text(json.dumps(generic), encoding="utf-8")
    cfg_path = tmp_path / "config.ofs"
    cfg_path.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP}\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path, TOP)
    pattern = ScanPattern(
        load_seqs={0: [False, False, False]},
        capture_pi_values={},
        expected_unload={0: [True, False, True]},
        unload_mask={0: [True, True, False]},
    )

    def gate(unload: list[bool]) -> None:
        monkeypatch.setattr(runner_mod, "_load_core", lambda: _FakeCore(unload))
        verify_golden_scan_protocol(
            cfg,
            manifest,
            netlist,
            pattern,
            vector_index=0,
            fault_id=None,
            reduced_vector={},
            functional_output_order=[],
        )

    gate([True, False, False])  # differs only at the don't-care bit
    with pytest.raises(RunnerError, match="unload"):
        gate([False, False, True])


@pytest.mark.unit
def test_unload_mask_round_trips_through_export() -> None:
    manifest = {"chains": [{"index": 0, "length": 3}], "max_chain_length": 3}
    pattern = serialize_vector(
        {"__ppo_ff1": True},
        _pseudo_port_map(),
        manifest,
        masked_ppo_ports=frozenset({"__ppo_ff1"}),
    )
    data = json.loads(json.dumps(scan_pattern_to_dict(pattern)))
    assert data["unload_mask"] == {"0": [True, False, True]}
    assert scan_pattern_from_dict(data) == pattern

    plain = serialize_vector({}, _pseudo_port_map(), manifest)
    assert scan_pattern_to_dict(plain)["unload_mask"] is None
    # A pattern file written before unload_mask existed still loads.
    legacy = scan_pattern_to_dict(plain)
    del legacy["unload_mask"]
    assert scan_pattern_from_dict(legacy).unload_mask is None
