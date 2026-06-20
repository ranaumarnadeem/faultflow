"""IEEE 1500 wrapper test mode — config + shell command (infrastructure step).

Covers the thin Python surface that feeds FaultflowConfig.test_mode:
  * `[testmode] mode` config section -> FaultflowConfig.test_mode (+ validation)
  * the fingerprint payload (a mode change invalidates resume)
  * ProjectSession.set_testmode state + persistence across reload
  * the set_testmode / report_testmode Tcl shell commands + help
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.runner.runner import Runner
from faultflow.shell.errors import ShellError
from faultflow.shell.help_text import COMMAND_HELP, render_help_overview
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


def _write_config(path: Path, testmode_section: str = "") -> None:
    path.write_text(
        ("""
[design]
netlist = demo.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[fault_model]
model = stuck_at
""" + testmode_section).strip() + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_testmode_default_is_functional(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    assert cfg.test_mode == "functional"


@pytest.mark.parametrize("mode", ["functional", "intest", "extest"])
def test_testmode_section_parsed(tmp_path: Path, mode: str) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, f"\n[testmode]\nmode = {mode}\n")
    cfg = load_config(cfg_path, "demo")
    assert cfg.test_mode == mode


def test_testmode_is_case_insensitive(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[testmode]\nmode = INTEST\n")
    cfg = load_config(cfg_path, "demo")
    assert cfg.test_mode == "intest"


def test_testmode_invalid_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[testmode]\nmode = bist\n")
    with pytest.raises(ConfigError):
        load_config(cfg_path, "demo")


# --------------------------------------------------------------------------- #
# fingerprint participation (resume invalidation)
# --------------------------------------------------------------------------- #
def test_testmode_change_changes_fingerprint(tmp_path: Path) -> None:
    func = tmp_path / "func.ofs"
    intest = tmp_path / "intest.ofs"
    _write_config(func)
    _write_config(intest, "\n[testmode]\nmode = intest\n")

    fp_func = Runner(load_config(func, "demo"))._config_fingerprint_payload()
    fp_intest = Runner(load_config(intest, "demo"))._config_fingerprint_payload()

    assert fp_func["test_mode"] == "functional"
    assert fp_intest["test_mode"] == "intest"
    assert fp_func != fp_intest


# --------------------------------------------------------------------------- #
# shell session + Tcl commands
# --------------------------------------------------------------------------- #
def _loaded_session(tmp_path: Path) -> ProjectSession:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


def test_session_set_testmode_default(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    assert session.report_testmode() == "functional"


def test_session_set_testmode_roundtrip(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.set_testmode("extest")
    assert session.report_testmode() == "extest"


def test_session_set_testmode_invalid_rejected(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    with pytest.raises(ShellError):
        session.set_testmode("scan")


def test_session_testmode_persists_across_reload(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.set_testmode("intest")
    reloaded = ProjectSession(output_root=tmp_path / "output")
    reloaded.load_session("demo")
    assert reloaded.report_testmode() == "intest"


def test_session_testmode_flows_into_materialized_config(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.set_testmode("intest")
    cfg = session.materialize_config()
    assert cfg.test_mode == "intest"


def test_tcl_set_testmode_and_report(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    bridge.eval("set_testmode intest")
    result = bridge.call("report_testmode")
    assert "intest" in str(result)


def test_tcl_set_testmode_usage_error(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    with pytest.raises(ShellError):
        bridge.call("set_testmode")


def test_tcl_set_testmode_invalid_value(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    with pytest.raises(ShellError):
        bridge.call("set_testmode", "bist")


# --------------------------------------------------------------------------- #
# help text
# --------------------------------------------------------------------------- #
def test_help_registers_testmode_commands() -> None:
    assert COMMAND_HELP["set_testmode"].category == "Test Mode"
    assert COMMAND_HELP["report_testmode"].category == "Test Mode"
    overview = render_help_overview()
    assert "set_testmode" in overview
    assert "report_testmode" in overview
