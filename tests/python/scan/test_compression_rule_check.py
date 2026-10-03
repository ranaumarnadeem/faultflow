from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.rule_check.model import Severity
from faultflow.rule_check.rules import rules_compression


def _manifest(tmp_path: Path, module: dict, compression: dict) -> dict:
    composed_json = tmp_path / "composed.json"
    composed_json.write_text(
        json.dumps({"modules": {"core_top_compressed": module}}), encoding="utf-8"
    )
    compression = dict(compression)
    if compression.get("enabled"):
        compression.setdefault("composed_json", str(composed_json))
        compression.setdefault("composed_top", "core_top_compressed")
    return {
        "top": "core_top",
        "compression": compression,
    }


@pytest.mark.unit
def test_rules_compression_empty_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, {"ports": {}, "cells": {}, "netnames": {}}, {"enabled": False}
    )
    assert rules_compression(manifest) == []


@pytest.mark.unit
def test_rules_compression_reports_comp001_on_mismatch(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": [1, 2]},
            "scan_in_0": {"bits": [1]},
        },
        "cells": {},
    }
    compression = {
        "enabled": True,
        "num_channels": 2,
        "tap_source_net": "effective_state",
        "scan_in_ports": ["scan_in_0"],
        # Declares taps=[0,1], but scan_in_0's net is literally bit 0 alone.
        "phase_shifter_taps": [[0, 1]],
    }
    manifest = _manifest(tmp_path, module, compression)

    violations = rules_compression(manifest)

    assert violations
    assert all(v.rule_id == "COMP001" for v in violations)
    assert any(v.severity == Severity.ERROR for v in violations)
