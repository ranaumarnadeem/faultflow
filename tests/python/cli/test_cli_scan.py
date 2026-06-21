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
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


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
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        }
    }


def _write_basic_config(tmp_path: Path, source: Path) -> Path:
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
    return cfg


def test_intest_extest_commands_force_mode_and_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `intest` / `extest` subcommands force [testmode] mode and run scan,
    regardless of the config's own test_mode (which defaults to functional)."""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(_tiny_dff_json(), indent=2) + "\n", encoding="utf-8")
    cfg = _write_basic_config(tmp_path, source)

    import faultflow.runner.runner as runner_mod

    captured: dict[str, object] = {}

    def fake_sim(self: object, **kwargs: object) -> str:
        captured["test_mode"] = getattr(self, "cfg").test_mode
        captured["scan"] = kwargs.get("scan")
        return "sim complete (stub)"

    monkeypatch.setattr(runner_mod.Runner, "sim", fake_sim)

    assert main(["intest", "--top", "tiny_dff", "-c", str(cfg)]) == 0
    assert captured == {"test_mode": "intest", "scan": True}

    captured.clear()
    assert main(["extest", "--top", "tiny_dff", "-c", str(cfg)]) == 0
    assert captured == {"test_mode": "extest", "scan": True}


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


def test_clean_workspace_removes_unified_and_legacy_scan_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--clean removes .faultflow/ and legacy root-level DB sidecars."""
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "output/tiny_dff"
    workspace = out / ".faultflow"
    workspace.mkdir(parents=True)
    comb_db = workspace / "faultflow.sqlite"
    legacy_comb = out / "faultflow.sqlite"
    scan_db = out / "faultflow_scan.sqlite"
    comb_db.write_text("comb", encoding="utf-8")
    legacy_comb.write_text("legacy", encoding="utf-8")
    scan_db.write_text("scan", encoding="utf-8")
    (out / "coverage.rpt").write_text("keep", encoding="utf-8")
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

    removed = Runner(runner_cfg)._clean_workspace()
    assert removed >= 2
    assert workspace.exists()
    assert not (workspace / "faultflow.sqlite").exists()
    assert not legacy_comb.exists()
    assert not scan_db.exists()
    assert (out / "coverage.rpt").exists()


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
