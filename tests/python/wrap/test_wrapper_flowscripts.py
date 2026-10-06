"""The Tcl flowscripts on the IEEE 1500 wrapper: core_atpg.tcl wraps a block Yosys
synthesizes, scans it and runs INTEST; extest.tcl resumes that session for the
block's EXTEST, a campaign of its own."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from wrap_flow import ACCUMULATOR

ROOT = Path(__file__).resolve().parents[3]
FLOWSCRIPTS = ROOT / "flowscripts"


@pytest.mark.integration
def test_core_atpg_then_extest_run_on_the_wrapper(
    tmp_path: Path, require_cpp_core: None
) -> None:
    from faultflow.cli import main

    if shutil.which("yosys") is None:
        pytest.skip("needs Yosys on PATH")
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    script = tmp_path / "flow.tcl"
    script.write_text(
        "\n".join(
            [
                "set ::_ff_scan_sourced 1",
                "set ::_ff_intest_sourced 1",
                "set ::_ff_core_sourced 1",
                f"source {{{FLOWSCRIPTS / 'core_atpg.tcl'}}}",
                "set ::_ff_extest_sourced 1",
                f"source {{{FLOWSCRIPTS / 'extest.tcl'}}}",
                f"ff::run_core_atpg top acc netlist {{{rtl}}} lib_cells sky130 "
                "clock_port clk scan_chains 1 atpg_max_rounds 5",
                "ff::run_extest top acc atpg_max_rounds 5",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # From the repository: the shell finds the sky130 cells there.
    assert main(["shell", "-f", str(script), "--out", str(tmp_path / "output")]) == 0
    (db,) = list((tmp_path / "output").rglob("*.sqlite"))
    conn = sqlite3.connect(db)
    detected = dict(
        conn.execute(
            "SELECT c.campaign_type, SUM(f.status = 'detected') FROM campaigns c "
            "JOIN faults f ON f.campaign_id = c.id GROUP BY c.campaign_type"
        ).fetchall()
    )
    conn.close()
    # INTEST in the block's scan campaign, EXTEST in its own: both test faults.
    assert set(detected) == {"scan", "scan_extest"}
    assert all(count > 0 for count in detected.values())
