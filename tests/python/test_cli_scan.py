from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.reports import manifest_from_result
from faultflow.scan import stitch_scan_json

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


def test_sim_scan_ext_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg = tmp_path / "config.ofs"
    cfg.write_text(
        f"""
[design]
netlist = {source}
cell_lib = {CELL_MAP}
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
""".strip() + "\n",
        encoding="utf-8",
    )
    ext = tmp_path / "vectors.test"
    ext.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["sim", "--scan", "--top", "tiny_dff", "-c", str(cfg), "--ext", str(ext)])
    assert exc.value.code == 2
    assert "pseudo-PI semantics" in capsys.readouterr().err


def test_sim_scan_clean_only_touches_scan_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "output/tiny_dff"
    out.mkdir(parents=True)
    comb_db = out / "faultflow.sqlite"
    scan_db = out / "faultflow_scan.sqlite"
    comb_db.write_text("comb", encoding="utf-8")
    scan_db.write_text("scan", encoding="utf-8")
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg = tmp_path / "config.ofs"
    cfg.write_text(
        f"""
[design]
netlist = {source}
cell_lib = {CELL_MAP}
[simulation]
unsupported_cells = fail
[atpg]
mode = comb
""".strip() + "\n",
        encoding="utf-8",
    )
    runner_cfg = load_config(cfg, "tiny_dff")
    from faultflow.runner.runner import Runner

    removed = Runner(runner_cfg)._clean_db(scan=True)
    assert removed == 1
    assert comb_db.exists()
    assert not scan_db.exists()


def test_reduced_view_fault_count_matches_manual(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    scanned = tmp_path / "tiny_dff_scan.json"
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", scanned)
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)
    generic = json.loads(scanned.read_text(encoding="utf-8"))
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_dff"]
    ports = module["ports"]
    assert "__ppi_u0" in ports
    assert "__ppo_u0" in ports
    assert len(port_map) == 1
