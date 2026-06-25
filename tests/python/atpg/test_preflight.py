"""Tests for OT _preflight subprocess wrapper (faultflow/testpoint/preflight.py).

Verifies that:
  - run_preflight returns None when opentest exits non-zero
  - run_preflight returns None when stdout has no JSON summary line
  - run_preflight returns PreflightData when stdout contains a valid summary
  - _parse_manifest extracts yosys_net_id values correctly
  - _parse_reconv_ids extracts stem_net_id values from the advanced format
  - items with null / missing yosys_net_id or stem_net_id are skipped
  - AtpgConfig picks up preflight / preflight_tech from config.ofs
  - session.set_option validates atpg.preflight as a boolean
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faultflow.testpoint.preflight import (
    PreflightData,
    _parse_manifest,
    _parse_reconv_ids,
    run_preflight,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(returncode: int = 0, stdout: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    return proc


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# run_preflight — subprocess routing
# ---------------------------------------------------------------------------


def test_run_preflight_returns_none_on_nonzero_exit(tmp_path: Path) -> None:
    netlist = tmp_path / "design.json"
    netlist.write_text("{}", encoding="utf-8")
    with patch("subprocess.run", return_value=_mock_proc(returncode=1)) as m:
        result = run_preflight(netlist, tmp_path / "pf", "opentest", "sky130")
    assert result is None
    m.assert_called_once()


def test_run_preflight_returns_none_when_no_json_line(tmp_path: Path) -> None:
    netlist = tmp_path / "design.json"
    netlist.write_text("{}", encoding="utf-8")
    proc = _mock_proc(stdout="progress line 1\nprogress line 2\n")
    with patch("subprocess.run", return_value=proc):
        result = run_preflight(netlist, tmp_path / "pf", "opentest", "sky130")
    assert result is None


def test_run_preflight_returns_none_on_oserror(tmp_path: Path) -> None:
    netlist = tmp_path / "design.json"
    netlist.write_text("{}", encoding="utf-8")
    with patch("subprocess.run", side_effect=OSError("not found")):
        result = run_preflight(netlist, tmp_path / "pf", "opentest", "sky130")
    assert result is None


def test_run_preflight_returns_none_on_status_not_ok(tmp_path: Path) -> None:
    netlist = tmp_path / "design.json"
    netlist.write_text("{}", encoding="utf-8")
    stdout = json.dumps({"status": "error", "manifest": "", "reconv_ids": ""})
    proc = _mock_proc(stdout=stdout)
    with patch("subprocess.run", return_value=proc):
        result = run_preflight(netlist, tmp_path / "pf", "opentest", "sky130")
    assert result is None


def test_run_preflight_parses_valid_output(tmp_path: Path) -> None:
    netlist = tmp_path / "design.json"
    netlist.write_text("{}", encoding="utf-8")

    manifest_path = tmp_path / "pf" / "tpi_manifest.json"
    reconv_path = tmp_path / "pf" / "design_reconv_ids.json"

    manifest_data = {
        "structural_hints": {
            "fanout_points": [
                {"name": "net_a", "yosys_net_id": 10},
                {"name": "net_b", "yosys_net_id": 20},
                {"name": "net_c", "yosys_net_id": None},  # null → skip
            ]
        }
    }
    reconv_data = {
        "reconvergences": [
            {
                "site": "g1",
                "pairs": [
                    {"stem_net_id": 30},
                    {"stem_net_id": None},  # null → skip
                ],
            }
        ]
    }
    _write_json(manifest_path, manifest_data)
    _write_json(reconv_path, reconv_data)

    stdout_json = json.dumps(
        {
            "status": "ok",
            "manifest": str(manifest_path),
            "reconv_ids": str(reconv_path),
            "tech": "sky130",
        }
    )
    proc = _mock_proc(stdout=f"progress\n{stdout_json}\n")
    with patch("subprocess.run", return_value=proc):
        result = run_preflight(netlist, tmp_path / "pf", "opentest", "sky130")

    assert result is not None
    assert isinstance(result, PreflightData)
    assert result.fanout_yosys_ids == frozenset({10, 20})
    assert result.redundant_stem_ids == frozenset({30})


# ---------------------------------------------------------------------------
# _parse_manifest
# ---------------------------------------------------------------------------


def test_parse_manifest_extracts_ids(tmp_path: Path) -> None:
    p = tmp_path / "manifest.json"
    data = {
        "structural_hints": {
            "fanout_points": [
                {"name": "a", "yosys_net_id": 1},
                {"name": "b", "yosys_net_id": 2},
                {"name": "c"},  # missing key → skip
                "plain_string",  # non-dict → skip
            ]
        }
    }
    _write_json(p, data)
    ids = _parse_manifest(p)
    assert ids == frozenset({1, 2})


def test_parse_manifest_empty_on_missing_file(tmp_path: Path) -> None:
    assert _parse_manifest(tmp_path / "nonexistent.json") == frozenset()


# ---------------------------------------------------------------------------
# _parse_reconv_ids
# ---------------------------------------------------------------------------


def test_parse_reconv_ids_extracts_stem_ids(tmp_path: Path) -> None:
    p = tmp_path / "reconv.json"
    data = {
        "reconvergences": [
            {"site": "s1", "pairs": [{"stem_net_id": 5}, {"stem_net_id": 6}]},
            {"site": "s2", "pairs": [{"stem_net_id": None}, {"stem_net_id": 7}]},
        ]
    }
    _write_json(p, data)
    ids = _parse_reconv_ids(p)
    assert ids == frozenset({5, 6, 7})


def test_parse_reconv_ids_empty_on_missing_file(tmp_path: Path) -> None:
    assert _parse_reconv_ids(tmp_path / "nonexistent.json") == frozenset()


# ---------------------------------------------------------------------------
# AtpgConfig picks up preflight fields from config file
# ---------------------------------------------------------------------------


def test_atpg_config_defaults() -> None:
    from faultflow.config import AtpgConfig

    cfg = AtpgConfig()
    assert cfg.preflight is True
    assert cfg.preflight_tech == ""


def test_load_config_preflight_fields(tmp_path: Path) -> None:
    from faultflow.config import load_config

    cfg_text = """
[design]
netlist   = tests/benchmarks/iscas85/synth_sky130/c17.json
top       = c17
cell_lib  = cells/sky130/sky130_fd_sc_hd.json
yosys_ver = 0.61

[fault_model]
model = stuck_at

[simulation]
unsupported_cells = fail

[atpg]
tool               = native
mode               = comb
preflight          = false
preflight_tech     = osu035

[report]
threshold = 95.0
"""
    cfg_file = tmp_path / "test.ofs"
    cfg_file.write_text(cfg_text, encoding="utf-8")
    cfg = load_config(cfg_file, "c17")
    assert cfg.atpg.preflight is False
    assert cfg.atpg.preflight_tech == "osu035"


# ---------------------------------------------------------------------------
# session.set_option validates atpg.preflight
# ---------------------------------------------------------------------------


def test_set_option_atpg_preflight_valid() -> None:
    from faultflow.shell.session import ProjectSession

    session = ProjectSession()
    for val in ("true", "false", "1", "0", "yes", "no"):
        session.set_option("atpg.preflight", val)
        assert session.options["atpg.preflight"] == val


def test_set_option_atpg_preflight_invalid() -> None:
    from faultflow.shell.errors import ShellError
    from faultflow.shell.session import ProjectSession

    session = ProjectSession()
    with pytest.raises(ShellError):
        session.set_option("atpg.preflight", "maybe")
