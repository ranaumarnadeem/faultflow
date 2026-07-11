"""CLI wiring smoke test for the `project` subcommand.

`project` is wired via argparse -> FlowService.run_project -> the hierarchical
orchestrator/aggregator, but was previously only ever exercised by calling
FlowService.run_project(...) directly (see test_project_aggregate.py). An
argparse wiring regression (e.g. a typo'd dest= on -p/--project, --max, -t, or
--clean) would go completely undetected without an actual
`faultflow.cli.main([...])` invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main
from soc2_fixtures import write_soc2


@pytest.mark.golden
def test_project_command_smoke(tmp_path: Path, require_cpp_core: None) -> None:
    manifest_path = write_soc2(tmp_path)

    assert main(["project", "-p", str(manifest_path)]) == 0

    out = tmp_path / "output" / "soc2"
    soc_json = out / "soc_coverage.json"
    assert soc_json.exists()
    report = json.loads(soc_json.read_text(encoding="utf-8"))
    assert report["schema"] == "faultflow_soc_coverage_v1"
    assert report["chip"]["denominator"] > 0
