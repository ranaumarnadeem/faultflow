"""Shell: `synth` auto-loads the synthesized JSON + the `load_json` command.

After `read_netlist design.v` + `synth`, the session must switch its active
design to the synthesized Yosys JSON so follow-up commands (check_cells,
add_scan, run_atpg, ...) operate on it without the user locating the output
path. `load_json` is the explicit entry point for loading a pre-synthesized JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.service import SynthesisResult
from faultflow.shell.errors import ShellError
from faultflow.shell.help_text import COMMAND_HELP, render_help_overview
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _tiny_netlist(top: str) -> dict:
    """One covered inverter + one uncovered $scopeinfo cell."""
    return {
        "modules": {
            top: {
                "attributes": {"top": "1"},
                "ports": {},
                "cells": {
                    "u_inv": {"type": "sky130_fd_sc_hd__inv_1", "connections": {}},
                    "u_info": {"type": "$scopeinfo", "connections": {}},
                },
                "netnames": {},
            }
        }
    }


class SynthService:
    """Minimal service: synth writes the gate-level JSON and reports its path."""

    def synthesize(self, cfg: object) -> SynthesisResult:
        out = cfg.intermediate_dir / f"{cfg.top}.json"  # type: ignore[attr-defined]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_tiny_netlist(cfg.top)), encoding="utf-8")  # type: ignore[attr-defined]  # noqa: E501
        return SynthesisResult(
            "synth",
            cfg.top,  # type: ignore[attr-defined]
            f"synthesis complete json={out}",
            artifacts={"netlist_json": out},
        )


def _verilog_session(tmp_path: Path) -> ProjectSession:
    src = tmp_path / "demo.v"
    src.write_text("module demo(); endmodule\n", encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "output", service=SynthService())
    session.read_netlist(src, "demo")
    session.use_lib_cells("sky130")
    return session


def test_synth_switches_active_design_to_synthesized_json(tmp_path: Path) -> None:
    session = _verilog_session(tmp_path)
    assert session.source_kind == "verilog"  # before synth

    result = session.synthesize()

    expected = session.materialize_config().intermediate_dir / "demo.json"
    assert session.source == expected
    assert session.source_kind == "yosys_json"
    assert session.synthesized is True
    assert "netlist_json" in result.artifacts
    # The checkpoint records the synthesized JSON, not the Verilog.
    saved = json.loads(session.checkpoint_path.read_text(encoding="utf-8"))
    assert saved["source"].endswith("demo.json")
    assert saved["source_kind"] == "yosys_json"


def test_check_cells_works_after_synth(tmp_path: Path) -> None:
    """The reported pain point: check_cells was unusable after synth because the
    session still pointed at the .v. Now it audits the synthesized JSON."""
    session = _verilog_session(tmp_path)
    session.synthesize()

    report = session.check_cells()

    assert report["total_cells"] == 2
    uncovered = [entry["type"] for entry in report["uncovered"]]  # type: ignore[index]
    assert "$scopeinfo" in uncovered


def test_load_json_loads_synthesized_netlist(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    source.write_text(json.dumps(_tiny_netlist("demo")), encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "output", service=SynthService())

    result = session.load_json(source, "demo")

    assert session.source == source
    assert session.source_kind == "yosys_json"
    assert session.synthesized is True
    assert result.operation == "read_netlist"


def test_load_json_rejects_verilog(tmp_path: Path) -> None:
    src = tmp_path / "demo.v"
    src.write_text("module demo(); endmodule\n", encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "output", service=SynthService())

    with pytest.raises(ShellError, match="use read_netlist for Verilog"):
        session.load_json(src, "demo")


def test_shell_load_json_command(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    source.write_text(json.dumps(_tiny_netlist("demo")), encoding="utf-8")
    session = ProjectSession(output_root=tmp_path / "output", service=SynthService())
    bridge = TclBridge(session)

    bridge.call("load_json", str(source), "-top", "demo")

    assert session.top == "demo"
    assert session.source_kind == "yosys_json"


def test_shell_load_json_bad_usage(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "output", service=SynthService())
    bridge = TclBridge(session)

    with pytest.raises(ShellError, match="usage: load_json"):
        bridge.call("load_json", "only_one_arg")


def test_help_registers_load_json() -> None:
    assert COMMAND_HELP["load_json"].category == "Project"
    assert "load_json PATH -top MODULE" in render_help_overview()
