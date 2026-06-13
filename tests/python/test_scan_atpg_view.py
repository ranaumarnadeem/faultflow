from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import ScanError, stitch_scan_json
from faultflow.scan.atpg_view import PPI_PREFIX, PPO_PREFIX, build_scan_atpg_view
from faultflow.scan.reports import manifest_from_result

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/osu/osu035.json"
MULTICHAIN_FIXTURE = ROOT / "tests/cpp/fixtures/tiny_scan_multichain.json"


def _manifest_for_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    data = json.loads(MULTICHAIN_FIXTURE.read_text(encoding="utf-8"))
    top = "tiny_scan_multichain"
    manifest = {
        "top": top,
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in_0", "scan_in_1"],
        "scan_outputs": ["scan_out_0", "scan_out_1"],
        "cells": [
            {
                "instance": "ff0",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 10,
                "data_net": 6,
            },
            {
                "instance": "ff1",
                "chain_index": 0,
                "chain_position": 1,
                "q_net": 11,
                "data_net": 7,
            },
            {
                "instance": "ff2",
                "chain_index": 1,
                "chain_position": 0,
                "q_net": 12,
                "data_net": 8,
            },
            {
                "instance": "ff3",
                "chain_index": 1,
                "chain_position": 1,
                "q_net": 13,
                "data_net": 9,
            },
        ],
    }
    return data, manifest


def test_pseudo_port_naming_and_direction() -> None:
    generic, manifest = _manifest_for_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_scan_multichain"]

    assert module["ports"]["__ppi_ff0"]["direction"] == "input"
    assert module["ports"]["__ppo_ff0"]["direction"] == "output"
    assert "ff0" not in module["cells"]
    assert port_map["ff0"]["ppi_port"] == "__ppi_ff0"
    assert port_map["ff0"]["ppo_port"] == "__ppo_ff0"


def test_collision_detection_aborts() -> None:
    generic, manifest = _manifest_for_fixture()
    module = generic["modules"]["tiny_scan_multichain"]
    module["ports"]["__ppi_ff0"] = {"direction": "input", "bits": [99]}

    with pytest.raises(ScanError, match="pseudo-port name already exists"):
        build_scan_atpg_view(generic, manifest)


def test_dangling_scan_ports_removed() -> None:
    generic, manifest = _manifest_for_fixture()
    view, _ = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_scan_multichain"]
    for name in (
        "scan_in_0",
        "scan_in_1",
        "scan_out_0",
        "scan_out_1",
        "scan_en",
        "CLK",
    ):
        assert name not in module["ports"]


def test_pseudo_port_ordering_matches_sorted_ff_instances() -> None:
    generic, manifest = _manifest_for_fixture()
    _, port_map = build_scan_atpg_view(generic, manifest)
    assert list(port_map.keys()) == ["ff0", "ff1", "ff2", "ff3"]


def test_pseudo_port_map_round_trips_manifest_cells() -> None:
    generic, manifest = _manifest_for_fixture()
    _, port_map = build_scan_atpg_view(generic, manifest)
    assert set(port_map) == {str(cell["instance"]) for cell in manifest["cells"]}
    for instance, entry in port_map.items():
        assert entry["chain_id"] in {0, 1}
        assert entry["position_in_chain"] in {0, 1}
        assert entry["ppi_port"] == f"{PPI_PREFIX}{instance}"
        assert entry["ppo_port"] == f"{PPO_PREFIX}{instance}"


def test_stitched_single_chain_view_is_valid(tmp_path: Path) -> None:
    tiny_dff = {
        "modules": {
            "tiny_dff": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "DFFPOSX1",
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
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        }
    }
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(tiny_dff, indent=2) + "\n", encoding="utf-8")
    output = tmp_path / "tiny_dff_scan.json"
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", output)
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)
    generic = json.loads(output.read_text(encoding="utf-8"))
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_dff"]
    assert len(port_map) == 1
    assert "scan_in" not in module["ports"]
    assert port_map["u0"]["ppi_port"] == "__ppi_u0"
