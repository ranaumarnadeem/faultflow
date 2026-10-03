from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.rule_check.model import Severity
from faultflow.rule_check.rules import rules_compaction


def _manifest(tmp_path: Path, module: dict, compaction: dict) -> dict:
    composed_json = tmp_path / "composed.json"
    composed_json.write_text(
        json.dumps({"modules": {"core_top_compacted": module}}), encoding="utf-8"
    )
    compaction = dict(compaction)
    if compaction.get("enabled"):
        compaction.setdefault("composed_json", str(composed_json))
        compaction.setdefault("composed_top", "core_top_compacted")
    return {
        "top": "core_top",
        "compaction": compaction,
    }


@pytest.mark.unit
def test_rules_compaction_empty_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, {"ports": {}, "cells": {}, "netnames": {}}, {"enabled": False}
    )
    assert rules_compaction(manifest) == []


@pytest.mark.unit
def test_rules_compaction_reports_comp002_on_mismatch(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "scan_out_0": {"bits": [1]},
            "scan_out_1": {"bits": [2]},
            "tdo": {"bits": [3]},
        },
        "cells": {
            "u_xor": {
                "type": "sky130_fd_sc_hd__xor2_1",
                "port_directions": {},
                "connections": {"A": [1], "B": [2], "X": [3]},
            }
        },
    }
    compaction = {
        "enabled": True,
        "num_outputs": 1,
        "scan_out_ports": ["scan_out_0", "scan_out_1"],
        "channel_port": "tdo",
        # Manifest claims tdo[0] = scan_out_0 only, but the netlist actually
        # XORs both chains together.
        "fanout": [[0]],
    }
    manifest = _manifest(tmp_path, module, compaction)

    violations = rules_compaction(manifest)

    assert violations
    assert all(v.rule_id == "COMP002" for v in violations)
    assert any(v.severity == Severity.ERROR for v in violations)
