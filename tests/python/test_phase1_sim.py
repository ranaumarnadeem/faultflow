from pathlib import Path

from faultflow.cli import main


def test_c17_existing_quaigh_test_flow() -> None:
    cfg = Path("config.ofs.example")

    assert main(["sim", "--top", "c17", "-c", str(cfg), "--purge"]) == 0
    assert Path("output/c17/fault_report.txt").exists()
    assert Path("output/c17/coverage_report.json").exists()
