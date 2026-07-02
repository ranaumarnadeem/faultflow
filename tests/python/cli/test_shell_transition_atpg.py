"""run_atpg -tf [broadside|los] unblocks transition-fault ATPG from the shell.

The core + runner already support transition ATPG; only tcl_bridge blocked it.
These tests pin (a) the bridge parsing of -tf and (b) that the session maps the
chosen launch onto the materialized config's fault model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.service import OperationResult
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _tiny_json(path: Path) -> None:
    # A .json source is treated as already-synthesized, so run_atpg's synth
    # precondition is satisfied without a real synthesis step.
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


class _CapturingService:
    """Captures the config + options handed to run_atpg. No write_report, so the
    session's _refresh_report is a no-op (no real campaign needed)."""

    def __init__(self) -> None:
        self.captured: tuple = ()

    def run_atpg(self, cfg: object, *, scan: bool = False, **options: object):
        self.captured = (cfg, scan, options)
        top = getattr(cfg, "top", "demo")
        return OperationResult("run_atpg", top, "ok")


def _ready_session(tmp_path: Path, service: object) -> ProjectSession:
    source = tmp_path / "demo.json"
    _tiny_json(source)
    session = ProjectSession(output_root=tmp_path / "output", service=service)
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    return session


def test_run_atpg_tf_no_longer_raises_unsupported(tmp_path: Path) -> None:
    session = _ready_session(tmp_path, _CapturingService())
    captured: dict = {}

    def fake_run_atpg(*, scan: bool = False, **options: object):
        captured["scan"] = scan
        captured.update(options)
        return OperationResult("run_atpg", "demo", "ok")

    session.run_atpg = fake_run_atpg  # type: ignore[method-assign]
    bridge = TclBridge(session)

    bridge._run_atpg(["-tf", "broadside"])
    assert captured.get("transition_model") == "broadside"

    captured.clear()
    bridge._run_atpg(["-tf", "los"])
    assert captured.get("transition_model") == "los"


def test_run_atpg_tf_rejects_bad_and_missing_mode(tmp_path: Path) -> None:
    session = _ready_session(tmp_path, _CapturingService())
    session.run_atpg = lambda **_kw: None  # type: ignore[method-assign]
    bridge = TclBridge(session)

    with pytest.raises(ShellError):
        bridge._run_atpg(["-tf", "bogus"])
    with pytest.raises(ShellError):
        bridge._run_atpg(["-tf"])


def test_session_run_atpg_maps_transition_model_onto_config(tmp_path: Path) -> None:
    service = _CapturingService()
    session = _ready_session(tmp_path, service)
    session.set_option("fault_model.collapsing", "false")

    session.run_atpg(transition_model="broadside")
    cfg, _scan, options = service.captured
    assert cfg.fault_model.model == "transition"
    assert cfg.fault_model.launch == "loc"  # broadside == launch-on-capture
    # transition_model is consumed by the session, not forwarded to the runner.
    assert "transition_model" not in options

    session.run_atpg(transition_model="los")
    cfg_los, _s, _o = service.captured
    assert cfg_los.fault_model.model == "transition"
    assert cfg_los.fault_model.launch == "los"


def test_session_run_atpg_defaults_to_stuck_at(tmp_path: Path) -> None:
    service = _CapturingService()
    session = _ready_session(tmp_path, service)

    session.run_atpg()
    cfg, _scan, _options = service.captured
    assert cfg.fault_model.model == "stuck_at"
