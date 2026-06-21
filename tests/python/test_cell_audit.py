"""`check_cells` — techmap cell-coverage audit shell command.

Covers the surface added for the cell-audit command:
  * faultflow.reporter.cell_audit.audit_netlist core logic
  * ProjectSession.check_cells (report-only, never aborts)
  * the check_cells Tcl shell command + -allow option + help

Mirrors the shell-layer style of test_clocks.py / test_testmode_flow.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.reporter.cell_audit import audit_netlist, cellmap_covers
from faultflow.shell.errors import ShellError
from faultflow.shell.help_text import COMMAND_HELP, render_help_overview
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

# A real Sky130 cell-map key matches covered cells; $scopeinfo is never covered.
SKY130_CELLMAP = Path("cells/sky130/sky130_fd_sc_hd.json")


def _netlist_with_uncovered(path: Path) -> None:
    """Tiny netlist: one covered inverter + one uncovered $scopeinfo cell."""
    path.write_text(
        json.dumps(
            {
                "modules": {
                    "demo": {
                        "attributes": {"top": "1"},
                        "ports": {},
                        "cells": {
                            "u_inv": {
                                "type": "sky130_fd_sc_hd__inv_1",
                                "connections": {},
                            },
                            "u_info": {
                                "type": "$scopeinfo",
                                "connections": {},
                            },
                        },
                        "netnames": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _loaded_session(tmp_path: Path) -> ProjectSession:
    source = tmp_path / "demo.json"
    _netlist_with_uncovered(source)
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


# --------------------------------------------------------------------------- #
# core audit logic
# --------------------------------------------------------------------------- #
def test_cellmap_covers_glob_and_exact() -> None:
    patterns = ["sky130_fd_sc_hd__inv_*", "$scanff_faultflow"]
    assert cellmap_covers("sky130_fd_sc_hd__inv_1", patterns)
    assert cellmap_covers("$scanff_faultflow", patterns)
    assert not cellmap_covers("$scopeinfo", patterns)


def test_audit_netlist_flags_uncovered(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    _netlist_with_uncovered(source)

    result = audit_netlist(source, SKY130_CELLMAP, [], top="demo")

    assert result.total_cells == 2
    assert not result.ok
    uncovered_types = [t for t, _ in result.uncovered]
    assert uncovered_types == ["$scopeinfo"]
    assert ("sky130_fd_sc_hd__inv_1", 1) not in result.uncovered


def test_audit_netlist_allow_list_clears(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    _netlist_with_uncovered(source)

    result = audit_netlist(source, SKY130_CELLMAP, ["$scopeinfo"], top="demo")

    assert result.ok
    assert result.uncovered == []
    assert [t for t, _ in result.allowed_uncovered] == ["$scopeinfo"]


# --------------------------------------------------------------------------- #
# session layer
# --------------------------------------------------------------------------- #
def test_session_check_cells_reports_uncovered(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    report = session.check_cells()

    assert report["total_cells"] == 2
    assert report["ok"] is False
    uncovered = [entry["type"] for entry in report["uncovered"]]  # type: ignore[index]
    assert "$scopeinfo" in uncovered


def test_session_check_cells_requires_design(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output")
    with pytest.raises(ShellError):
        session.check_cells()


def test_session_check_cells_does_not_abort(tmp_path: Path) -> None:
    """A failing audit must leave the session fully usable (report-only)."""
    session = _loaded_session(tmp_path)
    session.check_cells()
    # Session state survives: a follow-up command still works.
    assert session.top == "demo"
    assert session.report_testmode() == "functional"


# --------------------------------------------------------------------------- #
# shell layer
# --------------------------------------------------------------------------- #
def test_shell_check_cells_lists_uncovered(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    result = bridge.call("check_cells")

    uncovered = [entry["type"] for entry in result["uncovered"]]
    assert "$scopeinfo" in uncovered
    assert result["ok"] is False


def test_shell_check_cells_allow_option(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    result = bridge.call("check_cells", "-allow", "$scopeinfo")

    assert result["uncovered"] == []
    assert result["ok"] is True


def test_shell_check_cells_unknown_option_errors(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    with pytest.raises(ShellError):
        bridge.call("check_cells", "-bogus")


# --------------------------------------------------------------------------- #
# help text
# --------------------------------------------------------------------------- #
def test_help_registers_check_cells() -> None:
    assert COMMAND_HELP["check_cells"].category == "Project"
    overview = render_help_overview()
    assert "check_cells" in overview
