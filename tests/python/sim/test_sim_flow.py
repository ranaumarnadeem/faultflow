import json
from pathlib import Path

import pytest

from faultflow.cli import main
import faultflow.runner.runner as runner_mod


@pytest.mark.integration
@pytest.mark.slow
def test_c17_native_progressive_sim_flow() -> None:
    if runner_mod._load_core() is None:
        pytest.skip("C++ extension _faultflow_core is required")
    if not Path("tests/benchmarks/iscas85/synth_sky130/c17.json").exists():
        pytest.skip("c17 netlist missing")

    cfg = Path("config.ofs.example")
    Path("output/c17/.faultflow/faultflow.sqlite").unlink(missing_ok=True)

    assert main(["sim", "--top", "c17", "-c", str(cfg), "--purge", "--clean"]) == 0
    assert Path("output/c17/coverage.rpt").exists()
    report_path = Path("output/c17/.faultflow/intermediate/coverage_report.json")
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    # config.ofs.example enables compaction (default reverse), so the reported
    # deliverable run is the compacted one.
    assert report["run"]["vector_source"] == "compacted_native_sat_atpg"
    assert report["run"]["atpg_terminal_reason"] in {
        "COMPLETE",
        "THRESHOLD_MET",
        "STALLED",
        "MAX_ROUNDS",
    }
    assert report["run"]["atpg_rounds"] >= 1
