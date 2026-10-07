"""CLI wiring smoke test for the `project` subcommand.

`project` is wired via argparse -> FlowService.run_project -> the
faultflow_project_v2 flow (faultflow.project.soc_flow) and its aggregation. An
argparse wiring regression (e.g. a typo'd dest= on -p/--project, --max, -t,
--clean or --export-patterns) would go completely undetected without an actual
`faultflow.cli.main([...])` invocation.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.cli import main
from soc_v2_fixtures import write_soc_v2


@pytest.mark.integration
def test_project_command_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("needs Yosys on PATH")
    monkeypatch.chdir(tmp_path)
    manifest_path = write_soc_v2(tmp_path)
    patterns = tmp_path / "soc_extest.json"

    command = ["project", "-p", str(manifest_path), "--export-patterns", str(patterns)]
    assert main(command) == 0

    out = tmp_path / "output" / "soc2"
    report = json.loads((out / "soc_coverage.json").read_text(encoding="utf-8"))
    assert report["schema"] == "faultflow_soc_coverage_v2"
    assert [scope["name"] for scope in report["scopes"]] == ["blkA", "blkB", "soc"]
    assert report["chip"]["denominator"] > 0
    assert all(report["guards"][key] for key in ("scopes_distinct", "handoffs_owned"))
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    assert exported and all("shift_length" in pattern for pattern in exported)
