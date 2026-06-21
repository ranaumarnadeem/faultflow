from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _bridge(tmp_path: Path) -> TclBridge:
    return TclBridge(ProjectSession(output_root=tmp_path / "output"))


def test_help_overview_is_categorized_and_descriptive(tmp_path: Path) -> None:
    result = _bridge(tmp_path).call("help")
    text = str(result)

    assert "Project" in text
    assert "read_netlist PATH -top MODULE" in text
    assert "Load Verilog or Yosys JSON" in text
    assert "Scan" in text
    assert "add_scan -chains N" in text
    assert 'Use "help <command>" for details.' in text


def test_help_command_includes_usage_prerequisites_and_example(
    tmp_path: Path,
) -> None:
    result = _bridge(tmp_path).call("help", "add_scan")
    text = str(result)

    assert text.startswith("add_scan")
    assert "Usage: add_scan -chains N" in text
    assert "Requires:" in text
    assert "synthesized design" in text
    assert "does not run scan checking or techmap" in text
    assert "Example:" in text


def test_help_unknown_command_uses_invalid_option_error(tmp_path: Path) -> None:
    with pytest.raises(ShellError) as exc:
        _bridge(tmp_path).call("help", "missing")

    assert exc.value.code == ("FAULTFLOW", "CONFIG", "INVALID_OPTION")
    assert "unknown command" in str(exc.value)
