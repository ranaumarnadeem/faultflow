from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import load_config
from faultflow.service import FlowService, OperationResult
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def test_cli_serial_ref_delegates_to_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(load_config(Path("config.ofs.example"), "c17"), output_root=tmp_path)
    seen: dict[str, object] = {}

    class FakeService:
        def serial_reference(self, cfg_obj: object, *, scan: bool = False) -> object:
            seen["cfg"] = cfg_obj
            seen["scan"] = scan
            return OperationResult("serial_ref", "c17", "serial reference complete")

    monkeypatch.setattr("faultflow.cli.load_config", lambda _path, _top: cfg)
    monkeypatch.setattr(
        "faultflow.cli.FlowService", lambda runner_factory: FakeService()
    )

    assert (
        main(["sim", "--top", "c17", "-c", "config.ofs.example", "--serial-ref"]) == 0
    )

    assert seen["cfg"] is cfg
    assert seen["scan"] is False


def test_cli_scan_serial_ref_delegates_scan_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(load_config(Path("config.ofs.example"), "c17"), output_root=tmp_path)
    seen: dict[str, object] = {}

    class FakeService:
        def serial_reference(self, cfg_obj: object, *, scan: bool = False) -> object:
            seen["cfg"] = cfg_obj
            seen["scan"] = scan
            return OperationResult("serial_ref", "c17", "serial reference complete")

    monkeypatch.setattr("faultflow.cli.load_config", lambda _path, _top: cfg)
    monkeypatch.setattr(
        "faultflow.cli.FlowService", lambda runner_factory: FakeService()
    )

    assert (
        main(
            [
                "sim",
                "--top",
                "c17",
                "-c",
                "config.ofs.example",
                "--scan",
                "--serial-ref",
            ]
        )
        == 0
    )

    assert seen["scan"] is True


def test_service_serial_ref_requires_existing_campaign(tmp_path: Path) -> None:
    cfg = replace(load_config(Path("config.ofs.example"), "c17"), output_root=tmp_path)

    with pytest.raises(RuntimeError, match="run normal ATPG/sim first"):
        FlowService().serial_reference(cfg)


def test_shell_run_atpg_serial_ref_passes_reference_flag(tmp_path: Path) -> None:
    source = tmp_path / "demo.json"
    source.write_text(
        '{"modules":{"demo":{"ports":{},"cells":{},"netnames":{}}}}',
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class FakeService:
        def serial_reference(self, cfg_obj: object, *, scan: bool = False) -> object:
            seen["cfg"] = cfg_obj
            seen["scan"] = scan
            return OperationResult("serial_ref", "demo", "serial reference complete")

    session = ProjectSession(output_root=tmp_path / "output", service=FakeService())
    session.read_netlist(source, "demo")
    session.use_lib_cells("sky130")
    session.synthesized = True
    bridge = TclBridge(session)

    result = bridge.eval("run_atpg -serial_ref")

    assert "serial reference complete" in str(result)
    assert seen["scan"] is False
