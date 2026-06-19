"""Phase 6 — explicit clock declaration (`add_clock`).

Covers the three entry points that feed the multi-clock domain set:
  * `[clocks]` config section -> FaultflowConfig.clocks
  * ProjectSession.add_clock state + persistence + materialize_config
  * the `add_clock` / `report_clocks` Tcl shell commands + help
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import ClockSpec, ConfigError, load_config
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
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


def _write_config(path: Path, clocks_section: str = "") -> None:
    path.write_text(
        ("""
[design]
netlist = demo.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[fault_model]
model = stuck_at
""" + clocks_section).strip() + "\n",
        encoding="utf-8",
    )


def _loaded_session(tmp_path: Path) -> ProjectSession:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


# --------------------------------------------------------------------------- #
# config layer: [clocks]
# --------------------------------------------------------------------------- #
def test_clocks_section_parses_ports(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a, clk_b\n")

    cfg = load_config(cfg_path, "demo")

    assert cfg.clocks == (
        ClockSpec(port="clk_a", off_state=0),
        ClockSpec(port="clk_b", off_state=0),
    )


def test_clocks_off_state_override(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a, clk_b\noff = clk_b:1\n")

    cfg = load_config(cfg_path, "demo")

    assert cfg.clocks == (
        ClockSpec(port="clk_a", off_state=0),
        ClockSpec(port="clk_b", off_state=1),
    )


def test_no_clocks_section_is_empty(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path)

    assert load_config(cfg_path, "demo").clocks == ()


def test_clocks_invalid_off_state_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a\noff = clk_a:2\n")

    with pytest.raises(ConfigError, match="off"):
        load_config(cfg_path, "demo")


def test_clocks_duplicate_port_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a, clk_a\n")

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(cfg_path, "demo")


def test_clocks_off_unknown_port_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a\noff = clk_x:1\n")

    with pytest.raises(ConfigError, match="undeclared"):
        load_config(cfg_path, "demo")


# --------------------------------------------------------------------------- #
# config gate: transition + multi-clock is rejected (deferred follow-up)
# --------------------------------------------------------------------------- #
def test_transition_with_multiple_declared_clocks_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(
        cfg_path,
        "\n[clocks]\nports = clk_a, clk_b\n",
    )
    text = cfg_path.read_text(encoding="utf-8").replace(
        "model = stuck_at", "model = transition"
    )
    cfg_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="transition"):
        load_config(cfg_path, "demo")


def test_transition_with_single_declared_clock_allowed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[clocks]\nports = clk_a\n")
    text = cfg_path.read_text(encoding="utf-8").replace(
        "model = stuck_at", "model = transition"
    )
    cfg_path.write_text(text, encoding="utf-8")

    cfg = load_config(cfg_path, "demo")
    assert cfg.fault_model.model == "transition"


# --------------------------------------------------------------------------- #
# session layer
# --------------------------------------------------------------------------- #
def test_add_clock_appends_to_declared_clocks(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    session.add_clock("clk_a")
    session.add_clock("clk_b", off_state=1)

    assert session.declared_clocks == [
        ClockSpec(port="clk_a", off_state=0),
        ClockSpec(port="clk_b", off_state=1),
    ]


def test_add_clock_rejects_bad_off_state(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    with pytest.raises(ShellError):
        session.add_clock("clk_a", off_state=2)


def test_add_clock_replaces_duplicate_port(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    session.add_clock("clk_a", off_state=0)
    session.add_clock("clk_a", off_state=1)

    assert session.declared_clocks == [ClockSpec(port="clk_a", off_state=1)]


def test_declared_clocks_materialize_into_config(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)

    session.add_clock("clk_a")
    session.add_clock("clk_b", off_state=1)

    cfg = session.materialize_config()
    assert cfg.clocks == (
        ClockSpec(port="clk_a", off_state=0),
        ClockSpec(port="clk_b", off_state=1),
    )


def test_declared_clocks_persist_through_checkpoint(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.add_clock("clk_a")
    session.add_clock("clk_b", off_state=1)

    restored = ProjectSession(output_root=tmp_path / "output")
    restored.load_session("demo")

    assert restored.declared_clocks == [
        ClockSpec(port="clk_a", off_state=0),
        ClockSpec(port="clk_b", off_state=1),
    ]


def test_declared_clocks_in_checkpoint_json(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.add_clock("clk_a", off_state=1)

    data = json.loads(session.checkpoint_path.read_text(encoding="utf-8"))
    assert ["clk_a", 1] in [list(item) for item in data["declared_clocks"]]


# --------------------------------------------------------------------------- #
# shell layer
# --------------------------------------------------------------------------- #
def test_shell_add_clock_registers_clock(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    bridge.eval("add_clock clk_a")

    assert any(c.port == "clk_a" for c in session.declared_clocks)


def test_shell_add_clock_off_option(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)

    bridge.eval("add_clock clk_b -off 1")

    assert ClockSpec(port="clk_b", off_state=1) in session.declared_clocks


def test_shell_report_clocks_lists_declared(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    bridge.call("add_clock", "clk_a")
    bridge.call("add_clock", "clk_b", "-off", "1")

    result = bridge.call("report_clocks")

    ports = [entry["port"] for entry in result["clocks"]]
    assert ports == ["clk_a", "clk_b"]


def test_shell_add_clock_bad_off_errors(tmp_path: Path) -> None:
    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))

    with pytest.raises(ShellError) as exc:
        bridge.call("add_clock", "clk_a", "-off", "2")

    assert exc.value.code[0] == "FAULTFLOW"


def test_shell_add_clock_requires_port(tmp_path: Path) -> None:
    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))

    with pytest.raises(ShellError):
        bridge.call("add_clock")


def test_help_includes_add_clock(tmp_path: Path) -> None:
    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))

    overview = str(bridge.call("help"))
    detail = str(bridge.call("help", "add_clock"))

    assert "add_clock" in overview
    assert detail.startswith("add_clock")
    assert "Usage: add_clock PORT" in detail
