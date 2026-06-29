from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.service import ScanInsertionResult
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _tiny_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "modules": {
                    "demo": {
                        "attributes": {"top": "1"},
                        "ports": {},
                        "cells": {},
                        "netnames": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class FakeService:
    def __init__(self, fail_scan: bool = False) -> None:
        self.fail_scan = fail_scan
        self.scan_options: dict[str, object] = {}

    def insert_scan(self, cfg: object, **options: object) -> ScanInsertionResult:
        del cfg
        self.scan_options = options
        if self.fail_scan:
            raise RuntimeError("scan failed")
        return ScanInsertionResult("scan", "demo", "scan complete")


def test_failed_state_change_preserves_in_memory_and_checkpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output", service=FakeService())
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    before = session.snapshot()
    checkpoint = session.checkpoint_path
    before_text = checkpoint.read_text(encoding="utf-8")
    session.service = FakeService(fail_scan=True)

    with pytest.raises(RuntimeError, match="scan failed"):
        session.add_scan(scan_chains=2)

    assert session.snapshot() == before
    assert checkpoint.read_text(encoding="utf-8") == before_text


def test_successful_scan_commits_only_after_service_success(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    service = FakeService()
    session = ProjectSession(output_root=tmp_path / "output", service=service)
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")

    result = session.add_scan(scan_chains=3)

    assert result.message == "scan complete"
    assert session.scan_inserted is True
    assert session.scan_checked is False
    assert service.scan_options["scan_chains"] == 3
    assert json.loads(session.checkpoint_path.read_text())["scan_inserted"] is True


def test_scan_dry_run_does_not_commit_scan_state(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    service = FakeService()
    session = ProjectSession(output_root=tmp_path / "output", service=service)
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")

    session.add_scan(scan_chains=3, dry_run=True)

    assert session.scan_inserted is False
    assert json.loads(session.checkpoint_path.read_text())["scan_inserted"] is False


def test_set_option_materializes_into_config(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output", service=FakeService())
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")

    session.set_option("atpg.max_rounds", "37")
    session.set_option("report.threshold", "92.5")
    session.set_option("atpg.sat_timeout_schedule", "2,10,60")
    session.set_option("atpg.sat_conflict_limit", "2000000")
    session.set_option("fault_model.collapsing", "true")
    session.set_option("atpg.incremental_sat", "true")

    cfg = session.materialize_config()
    assert cfg.atpg.max_rounds == 37
    assert cfg.report.threshold == 92.5
    assert cfg.atpg.sat_timeout_schedule == "2,10,60"
    assert cfg.atpg.sat_conflict_limit == 2000000
    assert cfg.fault_model.collapsing is True
    assert cfg.atpg.incremental_sat is True


def test_shell_native_atpg_with_incremental_sat_reaches_full_coverage(
    tmp_path: Path,
) -> None:
    # End-to-end: the Tcl shell drives native (non-scan) ATPG with the IFC solver
    # enabled via set_option, reaching full coverage on a small combinational design.
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "comb.v"
    rtl.write_text(
        "module comb(input a, input b, input c, input dd, output y, output z);\n"
        "  assign y = (a & b) | c;\n"
        "  assign z = (a | dd) & ~(b & c);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "comb")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("set_option", "atpg.incremental_sat", "true")
    bridge.call("synth")
    bridge.call("run_atpg", "-target", "100.0")

    report = json.loads(
        (tmp_path / "out/comb/.faultflow/intermediate/coverage_report.json").read_text(
            encoding="utf-8"
        )
    )
    summary = report["summary"]
    assert summary["detected"] > 0
    assert summary["undetected"] == 0


def test_set_option_rejects_bad_timeout_schedule(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output", service=FakeService())
    with pytest.raises(ShellError, match="INVALID_VALUE|must be"):
        session.set_option("atpg.sat_timeout_schedule", "2,fast,60")
    with pytest.raises(ShellError, match="INVALID_VALUE|>= 1"):
        session.set_option("atpg.sat_timeout_schedule", "0,10")


def test_tcl_catch_receives_stable_faultflow_error_code(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output", service=FakeService())
    bridge = TclBridge(session)

    result = bridge.eval(
        "set rc [catch {run_atpg -scan} msg opts]; "
        "list $rc $msg [dict get $opts -errorcode]"
    )

    assert str(result).startswith("1 ")
    assert "FAULTFLOW PRECONDITION NO_DESIGN" in str(result)


def test_native_tcl_procedures_and_loops_remain_available(tmp_path: Path) -> None:
    bridge = TclBridge(
        ProjectSession(output_root=tmp_path / "output", service=FakeService())
    )

    result = bridge.eval(
        "proc add {a b} {expr {$a + $b}}; "
        "set total 0; foreach n {1 2 3} {incr total $n}; "
        "list [add 2 5] $total"
    )

    assert str(result) == "7 6"


def test_write_patterns_is_registered_but_unsupported(tmp_path: Path) -> None:
    bridge = TclBridge(
        ProjectSession(output_root=tmp_path / "output", service=FakeService())
    )

    with pytest.raises(ShellError) as exc:
        bridge.call("write_patterns")

    assert exc.value.code == ("FAULTFLOW", "UNSUPPORTED", "PATTERN_EXPORT")
