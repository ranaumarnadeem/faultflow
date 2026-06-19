from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.detection_pipeline import _materialize_reduced_outputs
from faultflow.scan.protocol import serialize_vector
from faultflow.scan.site_resolution import build_scan_execution_map

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests/fixtures/scan_protocol/single_chain"
GENERIC = FIXTURE_ROOT / "generic.json"
MANIFEST = FIXTURE_ROOT / "manifest.json"
CASES = FIXTURE_ROOT / "cases.json"
SCHEMA = ROOT / "schemas/scan_protocol_fixture.schema.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _case() -> dict[str, Any]:
    cases = _load(CASES)["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 1
    row = cases[0]
    assert isinstance(row, dict)
    return row


def _chain_state_from_load(load_bits: list[bool], chain_length: int) -> list[bool]:
    """Independent serial-chain oracle: position 0 is nearest scan-in."""
    state = [False] * chain_length
    for bit in load_bits:
        state = [bit, *state[:-1]]
    return state


def _unload_from_state(state: list[bool]) -> list[bool]:
    """Independent oracle: scan-out observes the highest position first."""
    return list(reversed(state))


@pytest.mark.unit
def test_scan_protocol_fixture_matches_schema() -> None:
    schema = _load(SCHEMA)
    fixture = _load(CASES)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["version"]["const"] == fixture["version"]
    assert set(schema["required"]).issubset(fixture)
    assert fixture["top"]
    assert fixture["cases"]


@pytest.mark.unit
def test_fixture_chain_equations_are_self_consistent() -> None:
    case = _case()
    load_seqs = case["load_seqs"]
    expected_unload = case["expected_unload"]
    assert isinstance(load_seqs, dict)
    assert isinstance(expected_unload, dict)
    loaded = _chain_state_from_load(list(load_seqs["0"]), 2)
    assert loaded == [True, False]
    captured = [False, True]
    assert _unload_from_state(captured) == list(expected_unload["0"])


@pytest.mark.golden
def test_reduced_view_matches_independent_fixture_expectations(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic = _load(GENERIC)
    manifest = _load(MANIFEST)
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    case = _case()
    vector = dict(case["reduced_vector"])
    input_order = [
        name
        for name, port in reduced["modules"]["scan_protocol_single"]["ports"].items()
        if port["direction"] == "input"
    ]
    output_order = ["Y", "__ppo_ff0", "__ppo_ff1"]
    inputs = {name: bool(vector[name]) for name in input_order}
    outputs = core.fault_free_outputs(
        str(reduced_path),
        str(CELL_MAP),
        [inputs],
        input_order,
        output_order,
        "fail",
    )

    expected = {
        **dict(case["pre_capture_outputs"]),
        **dict(case["captured_ppo"]),
    }
    assert outputs == [expected]

    pattern = serialize_vector(vector, port_map, manifest)
    assert pattern.load_seqs == {0: list(case["load_seqs"]["0"])}
    assert pattern.expected_unload == {0: list(case["expected_unload"]["0"])}


@pytest.mark.golden
def test_input_only_candidate_materializes_real_po_and_ppo_values(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic = _load(GENERIC)
    manifest = _load(MANIFEST)
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    case = _case()
    full_vector = dict(case["reduced_vector"])
    input_order = [
        name
        for name, port in reduced["modules"]["scan_protocol_single"]["ports"].items()
        if port["direction"] == "input"
    ]
    input_only = {name: bool(full_vector[name]) for name in input_order}
    materialized = _materialize_reduced_outputs(
        core,
        str(reduced_path),
        str(CELL_MAP),
        input_only,
        input_order,
        ["Y"],
        port_map,
        "fail",
    )

    assert materialized["Y"] is True
    assert materialized["__ppo_ff0"] is False
    assert materialized["__ppo_ff1"] is True


@pytest.mark.golden
def test_physical_protocol_observes_pre_capture_outputs(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    case = _case()
    result = core.simulate_scan_pattern(
        str(GENERIC),
        str(CELL_MAP),
        ["CLK"],
        scan_enable_port="scan_en",
        scan_input_ports=["scan_in"],
        scan_output_ports=["scan_out"],
        functional_output_ports=["Y"],
        max_chain_length=2,
        load_seqs={0: list(case["load_seqs"]["0"])},
        capture_pi_values={"A": False, "D0": False, "D1": True},
        unsupported_policy="fail",
    )

    assert result["real_po_values"] == dict(case["pre_capture_outputs"])
    assert result["unload_seqs"] == {0: list(case["expected_unload"]["0"])}
    assert result["real_po_values"] != dict(case["post_capture_outputs"])


@pytest.mark.golden
def test_scan_execution_map_uses_exact_exclusion_categories(
    tmp_path: Path, require_cpp_core: None
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    generic = _load(GENERIC)
    manifest = _load(MANIFEST)
    reduced, port_map = build_scan_atpg_view(generic, manifest)
    reduced_path = tmp_path / "reduced.json"
    reduced_path.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    _, exclusions = build_scan_execution_map(
        core,
        GENERIC,
        reduced_path,
        CELL_MAP,
        CELL_MAP,
        "fail",
        port_map,
        manifest,
    )

    assert exclusions["net:8:branch:ff1:SDI"] == "scan_internal"
    assert exclusions["net:3:stem"] == "scan_chain"
    assert exclusions["net:4:stem"] == "scan_internal"
