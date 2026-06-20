"""M1 — Path B oracle: `faultflow run --config <ot.ofs>` → oracle_response.json.

OT calls faultflow with an OT-shaped ofs; faultflow sims and writes a flat
oracle_response.json that OT's bridge.read_oracle_response() consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.service.oracle import (
    build_oracle_response,
    discover_manifest,
    map_terminal_reason,
    translate_oracle_ofs,
    write_oracle_response,
)

ROOT = Path(__file__).resolve().parents[2]
TINY_NETLIST = ROOT / "tests/fixtures/osu035/tiny_mx2x1.json"
OSU_CELL_MAP = ROOT / "cells/osu/osu035.json"
SCHEMA_PATH = ROOT / "schemas/oracle_response.schema.json"
TOP = "tiny_mx2x1"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _write_ot_ofs(
    tmp_path: Path,
    netlist: Path,
    top: str,
    *,
    extra_sections: str = "",
    cell_lib: str | None = None,
) -> Path:
    ofs_path = tmp_path / "faultflow.ofs"
    cell_line = f"cell_lib = {cell_lib}" if cell_lib else ""
    ofs_path.write_text(
        f"""
[input]
netlist = {netlist}

[design]
top_module = {top}
{cell_line}

[output]
dir = {tmp_path}

{extra_sections}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return ofs_path


def _write_manifest(
    ofs_path: Path,
    response_path: Path,
    *,
    session_id: str = "sess_abc",
    iteration: int = 1,
) -> None:
    manifest = {
        "session_id": session_id,
        "iteration": iteration,
        "netlist_path": str(TINY_NETLIST),
        "response_path": str(response_path),
    }
    (ofs_path.parent / "tpi_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# M1-U01 — translate_oracle_ofs maps OT sections to faultflow cfg
# --------------------------------------------------------------------------- #

def test_translate_oracle_ofs_netlist(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP)
    cfg, top = translate_oracle_ofs(ofs)
    assert cfg.netlist == TINY_NETLIST
    assert top == TOP


def test_translate_oracle_ofs_top(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, "my_design")
    _cfg, top = translate_oracle_ofs(ofs)
    assert top == "my_design"


def test_translate_oracle_ofs_cell_lib_default(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP)
    cfg, _ = translate_oracle_ofs(ofs)
    assert "sky130" in cfg.cell_lib.name


def test_translate_oracle_ofs_cell_lib_explicit(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP, cell_lib=str(OSU_CELL_MAP))
    cfg, _ = translate_oracle_ofs(ofs)
    assert cfg.cell_lib == OSU_CELL_MAP


def test_translate_oracle_ofs_output_root(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP)
    cfg, _ = translate_oracle_ofs(ofs)
    assert cfg.output_root == tmp_path


# --------------------------------------------------------------------------- #
# M1-U02 — discover_manifest
# --------------------------------------------------------------------------- #

def test_discover_manifest_present(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP)
    response_path = tmp_path / "oracle_response.json"
    _write_manifest(ofs, response_path, session_id="xyz", iteration=3)
    manifest = discover_manifest(ofs)
    assert manifest is not None
    assert manifest["session_id"] == "xyz"
    assert manifest["iteration"] == 3
    assert manifest["response_path"] == str(response_path)


def test_discover_manifest_absent(tmp_path: Path) -> None:
    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP)
    assert discover_manifest(ofs) is None


# --------------------------------------------------------------------------- #
# M1-U03 — map_terminal_reason
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "faultflow_reason,expected",
    [
        ("target_reached", "target_reached"),
        ("exhausted", "exhausted"),
        ("stalled", "exhausted"),
        ("timeout", "timeout"),
        ("unknown", "timeout"),
        ("", "exhausted"),
    ],
)
def test_map_terminal_reason(faultflow_reason: str, expected: str) -> None:
    assert map_terminal_reason(faultflow_reason) == expected


# --------------------------------------------------------------------------- #
# M1-U04 — build_oracle_response produces flat schema
# --------------------------------------------------------------------------- #

def _fake_report(
    fault_coverage: float = 75.0,
    vector_count: int = 10,
    terminal: str = "exhausted",
) -> dict:
    return {
        "summary": {
            "fault_coverage_percent": fault_coverage,
            "test_coverage_percent": fault_coverage,
            "coverage_percent": fault_coverage,
            "detected": 3,
            "denominator": 4,
            "undetected": 1,
        },
        "run": {
            "vector_count": vector_count,
            "atpg_terminal_reason": terminal,
        },
        "per_node": [{"net_id": 5, "net_name": "Y", "detected": 1, "undetected": 1}],
        "undetected_faults": [{"id": 1, "net_name": "Y", "fault_type": "SA0"}],
    }


def test_build_oracle_response_no_manifest() -> None:
    resp = build_oracle_response(_fake_report(), manifest=None)
    assert resp["backend"] == "faultflow"
    assert resp["coverage_percent"] == pytest.approx(75.0)
    assert resp["fault_coverage_percent"] == pytest.approx(75.0)
    assert resp["vector_count"] == 10
    assert resp["terminal_reason"] == "exhausted"
    assert len(resp["per_node"]) == 1
    assert len(resp["undetected_faults"]) == 1
    assert "session_id" not in resp
    assert "iteration" not in resp


def test_build_oracle_response_with_manifest() -> None:
    manifest = {"session_id": "abc", "iteration": 2, "response_path": "/tmp/r.json"}
    resp = build_oracle_response(_fake_report(), manifest=manifest)
    assert resp["session_id"] == "abc"
    assert resp["iteration"] == 2


def test_build_oracle_response_terminal_mapped() -> None:
    resp = build_oracle_response(_fake_report(terminal="stalled"), manifest=None)
    assert resp["terminal_reason"] == "exhausted"


def test_build_oracle_response_target_reached() -> None:
    resp = build_oracle_response(_fake_report(terminal="target_reached"), manifest=None)
    assert resp["terminal_reason"] == "target_reached"


# --------------------------------------------------------------------------- #
# M1-U05 — schema validation
# --------------------------------------------------------------------------- #

def test_oracle_response_schema_valid() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    resp = build_oracle_response(_fake_report(), manifest=None)
    jsonschema.validate(resp, schema)


def test_write_oracle_response_creates_file(tmp_path: Path) -> None:
    resp = build_oracle_response(_fake_report(), manifest=None)
    out = tmp_path / "oracle_response.json"
    write_oracle_response(resp, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["backend"] == "faultflow"
    assert loaded["coverage_percent"] == pytest.approx(75.0)


# --------------------------------------------------------------------------- #
# M1-G01 — golden: run_oracle end-to-end writes valid oracle_response.json
# --------------------------------------------------------------------------- #

@pytest.mark.golden
def test_run_oracle_exit_0(tmp_path: Path, require_cpp_core: None) -> None:
    from faultflow.service.oracle import run_oracle

    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP, cell_lib=str(OSU_CELL_MAP))
    response_path = tmp_path / "oracle_response.json"
    _write_manifest(ofs, response_path, session_id="test_sess", iteration=1)

    rc = run_oracle(ofs)
    assert rc == 0, f"run_oracle returned {rc}"

    assert response_path.exists(), "oracle_response.json not written"
    resp = json.loads(response_path.read_text(encoding="utf-8"))
    assert resp["backend"] == "faultflow"
    assert resp["session_id"] == "test_sess"
    assert resp["iteration"] == 1
    assert resp["coverage_percent"] >= 0.0
    assert isinstance(resp["per_node"], list)
    assert isinstance(resp["undetected_faults"], list)


@pytest.mark.golden
def test_run_oracle_no_manifest_default_path(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.service.oracle import run_oracle

    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP, cell_lib=str(OSU_CELL_MAP))
    # No manifest — response goes to <output_dir>/oracle_response.json
    rc = run_oracle(ofs)
    assert rc == 0

    default_path = tmp_path / "oracle_response.json"
    assert default_path.exists()
    resp = json.loads(default_path.read_text(encoding="utf-8"))
    assert resp["backend"] == "faultflow"
    assert "session_id" not in resp


@pytest.mark.golden
def test_cli_run_command_exit_0(tmp_path: Path, require_cpp_core: None) -> None:
    from faultflow.cli import main

    ofs = _write_ot_ofs(tmp_path, TINY_NETLIST, TOP, cell_lib=str(OSU_CELL_MAP))
    rc = main(["run", "-c", str(ofs)])
    assert rc == 0
    assert (tmp_path / "oracle_response.json").exists()
