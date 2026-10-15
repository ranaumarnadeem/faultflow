from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.rule_check.model import Severity
from faultflow.rule_check.rules import rules_compression
from faultflow.scan.compression import build_continuous_decompressor

CELL_MAP = Path(__file__).resolve().parents[3] / "cells/sky130/sky130_fd_sc_hd.json"


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
        "max_chain_length": 3,
        "compression": compression,
    }


@pytest.mark.unit
def test_rules_compression_empty_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, {"ports": {}, "cells": {}, "netnames": {}}, {"enabled": False}
    )
    assert rules_compression(manifest, CELL_MAP) == []


@pytest.mark.unit
def test_rules_compression_reports_comp001_on_mismatch(tmp_path: Path) -> None:
    """A composed module with none of the decompressor the entry describes."""
    entry = build_continuous_decompressor(
        ["scan_in_0"], 2, ring_width=8, warmup=2
    ).manifest_entry()
    module: dict = {"ports": {}, "netnames": {}, "cells": {}}
    manifest = _manifest(tmp_path, module, {"enabled": True, **entry})

    violations = rules_compression(manifest, CELL_MAP)

    assert violations
    assert all(v.rule_id == "COMP001" for v in violations)
    assert any(v.severity == Severity.ERROR for v in violations)


@pytest.mark.unit
def test_rules_compression_says_to_scan_compress_again_for_a_one_shot_entry(
    tmp_path: Path,
) -> None:
    """A manifest from the one-shot decompressor has no architecture."""
    entry = build_continuous_decompressor(
        ["scan_in_0"], 2, ring_width=8, warmup=2
    ).manifest_entry()
    del entry["architecture"]
    module: dict = {"ports": {}, "netnames": {}, "cells": {}}
    manifest = _manifest(tmp_path, module, {"enabled": True, **entry})

    violations = rules_compression(manifest, CELL_MAP)

    assert [v.rule_id for v in violations] == ["COMP001"]
    assert violations[0].severity == Severity.ERROR
    assert "scan-compress" in violations[0].message
