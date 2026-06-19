"""Phase 9 — instance blackboxing config + shell command.

Covers the entry points that feed FaultflowConfig.blackbox_instances:
  * `[blackbox]` config section -> FaultflowConfig.blackbox_instances
  * the fingerprint payload (a blackbox change invalidates resume)
  * ProjectSession.add_blackbox state + persistence
  * the add_blackbox / report_blackbox Tcl shell commands + help
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.runner.runner import Runner
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


def _write_config(path: Path, blackbox_section: str = "") -> None:
    path.write_text(
        ("""
[design]
netlist = demo.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[fault_model]
model = stuck_at
""" + blackbox_section).strip() + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 9-P01 / 9-P02 — config parsing
# --------------------------------------------------------------------------- #
def test_blackbox_section_parsed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[blackbox]\ninstances = u_a, u_b\n")
    cfg = load_config(cfg_path, "demo")
    assert cfg.blackbox_instances == ("u_a", "u_b")


def test_no_blackbox_section_is_empty(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path)
    cfg = load_config(cfg_path, "demo")
    assert cfg.blackbox_instances == ()


def test_blackbox_duplicate_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[blackbox]\ninstances = u_a, u_a\n")
    with pytest.raises(ConfigError):
        load_config(cfg_path, "demo")


def test_blackbox_empty_instances_is_empty(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ofs"
    _write_config(cfg_path, "\n[blackbox]\ninstances =\n")
    cfg = load_config(cfg_path, "demo")
    assert cfg.blackbox_instances == ()


# --------------------------------------------------------------------------- #
# 9-P03 — fingerprint participation (resume invalidation)
# --------------------------------------------------------------------------- #
def test_blackbox_change_changes_fingerprint(tmp_path: Path) -> None:
    plain = tmp_path / "plain.ofs"
    boxed = tmp_path / "boxed.ofs"
    _write_config(plain)
    _write_config(boxed, "\n[blackbox]\ninstances = u_sram\n")

    fp_plain = Runner(load_config(plain, "demo"))._config_fingerprint_payload()
    fp_boxed = Runner(load_config(boxed, "demo"))._config_fingerprint_payload()

    assert fp_plain["blackbox_instances"] == []
    assert fp_boxed["blackbox_instances"] == ["u_sram"]
    assert fp_plain != fp_boxed


# --------------------------------------------------------------------------- #
# 9-P04 — shell session + Tcl commands
# --------------------------------------------------------------------------- #
def _loaded_session(tmp_path: Path) -> ProjectSession:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output")
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


def test_session_add_blackbox_accumulates_and_dedups(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.add_blackbox("u_sram")
    session.add_blackbox("u_pll")
    session.add_blackbox("u_sram")  # duplicate ignored
    assert session.report_blackbox() == ["u_sram", "u_pll"]


def test_session_add_blackbox_empty_rejected(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    with pytest.raises(ShellError):
        session.add_blackbox("   ")


def test_session_blackbox_persists_across_reload(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    session.add_blackbox("u_sram")
    reloaded = ProjectSession(output_root=tmp_path / "output")
    reloaded.load_session("demo")
    assert reloaded.report_blackbox() == ["u_sram"]


def test_tcl_add_blackbox_and_report(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    bridge.eval("add_blackbox u_sram")
    result = bridge.call("report_blackbox")
    assert "u_sram" in str(result)


def test_tcl_add_blackbox_usage_error(tmp_path: Path) -> None:
    session = _loaded_session(tmp_path)
    bridge = TclBridge(session)
    with pytest.raises(ShellError):
        bridge.call("add_blackbox")
