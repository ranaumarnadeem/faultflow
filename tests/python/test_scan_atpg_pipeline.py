from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.atpg import VectorSet
from faultflow.config import load_config
from faultflow.runner import Runner, RunnerError
from faultflow.runner.progressive_atpg import AtpgStats
from faultflow.scan import stitch_scan_json
from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/osu/osu035.json"


def _tiny_dff_json() -> dict[str, object]:
    return {
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


def _write_config(path: Path, netlist: Path) -> Path:
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


def _install_passing_scan_workspace(
    tmp_path: Path,
) -> tuple[Runner, Path, dict[str, object]]:
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg_path = _write_config(tmp_path / "config.ofs", source)
    cfg = load_config(cfg_path, "tiny_dff")
    cfg.ensure_workspace()
    generic = cfg.scan_json_path
    techmap = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
    techmap.write_text("// test techmap\n", encoding="utf-8")
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", generic)
    manifest = manifest_from_result(result, source, techmap, None)
    generic_hash = hash_file(generic)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": generic_hash,
    }
    manifest_path = cfg.scan_manifest_path
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return Runner(cfg), generic, manifest


def _stub_fingerprint(_netlist: Path) -> dict[str, object]:
    return {
        "netlist_hash": "scan-view-hash",
        "cell_lib_hash": "cell-lib-hash",
        "config_hash": "config-hash",
        "template_hash": "template-hash",
        "yosys_version": "yosys",
        "faultflow_version": "test",
        "collapsing": 0,
        "unsupported_cells": "fail",
        "include_clock_faults": 0,
        "include_reset_faults": 0,
    }


def test_sim_scan_passes_scan_db_to_progressive_atpg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, _generic, _manifest = _install_passing_scan_workspace(tmp_path)
    captured: dict[str, object] = {}

    def fake_progressive(
        cfg: object,
        netlist: Path,
        model_id: str,
        **kwargs: object,
    ) -> tuple[VectorSet, AtpgStats, int, float, float]:
        del cfg, model_id
        captured["campaign_id"] = kwargs.get("campaign_id")
        captured["netlist"] = netlist
        captured["vector_source"] = kwargs.get("vector_source")
        captured["scan_ctx"] = kwargs.get("scan_ctx")
        return (
            VectorSet("scan_native_sat_atpg", ["D"], [{"D": False}]),
            AtpgStats(terminal_reason="COMPLETE"),
            1,
            0.0,
            0.0,
        )

    monkeypatch.setattr(
        "faultflow.runner.progressive_atpg.run_progressive_native_atpg",
        fake_progressive,
    )
    monkeypatch.setattr(
        Runner, "_fingerprint", lambda self, netlist: _stub_fingerprint(netlist)
    )
    monkeypatch.setattr(
        Runner, "_ensure_campaign", lambda self, conn, fp, scan=False: 1
    )
    monkeypatch.setattr(
        "faultflow.runner.runner.write_reports",
        lambda conn, cfg, scan_context=None, campaign_id=None: (
            cfg.coverage_json_path,
            cfg.coverage_report_path,
            {
                "summary": {"coverage_percent": 0.0},
                "metadata": {"scan_mode": bool(scan_context)},
            },
        ),
    )

    result = runner.sim(scan=True)
    assert captured["campaign_id"] == 1
    assert captured["vector_source"] == "scan_native_sat_atpg"
    assert captured["scan_ctx"] is not None
    netlist = captured["netlist"]
    assert isinstance(netlist, Path)
    assert netlist.name == "scan_atpg_view.json"
    assert "mode=scan" in result


def test_preflight_sim_scan_missing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg = load_config(_write_config(tmp_path / "config.ofs", source), "tiny_dff")
    runner = Runner(cfg)
    with pytest.raises(RunnerError, match="scan manifest not found"):
        runner._preflight_sim_scan()


def test_preflight_sim_scan_requires_passing_scan_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, _generic, manifest = _install_passing_scan_workspace(tmp_path)
    manifest["latest_check"] = {"status": "FAIL", "errors": ["boom"]}
    manifest_path = runner.cfg.scan_manifest_path
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RunnerError, match="scan-check has not passed"):
        runner._preflight_sim_scan()


def test_preflight_sim_scan_rejects_stale_check_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, generic, manifest = _install_passing_scan_workspace(tmp_path)
    latest = manifest["latest_check"]
    assert isinstance(latest, dict)
    latest["generic_json_hash"] = "stale-hash"
    manifest_path = runner.cfg.scan_manifest_path
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assert hash_file(generic) != "stale-hash"
    with pytest.raises(RunnerError, match="scan-check is stale"):
        runner._preflight_sim_scan()


def test_preflight_sim_scan_rejects_ineligible_ffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, _generic, manifest = _install_passing_scan_workspace(tmp_path)
    manifest["ineligible_ffs"] = [
        {"instance": "u_bad", "cell_type": "DFFPOSX1", "reason": "unsupported_ff_shape"}
    ]
    manifest_path = runner.cfg.scan_manifest_path
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RunnerError, match="ineligible FFs remain"):
        runner._preflight_sim_scan()


def test_comb_and_scan_share_unified_db_path(tmp_path: Path) -> None:
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg = load_config(_write_config(tmp_path / "config.ofs", source), "tiny_dff")
    assert cfg.db_path.name == "faultflow.sqlite"
    assert cfg.db_path == cfg.workspace_dir / "faultflow.sqlite"
