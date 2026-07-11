from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.shell.errors import ShellError
from faultflow.shell.help_text import COMMAND_HELP
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _bridge(tmp_path: Path) -> TclBridge:
    return TclBridge(ProjectSession(output_root=tmp_path / "output"))


def test_every_registered_command_has_help_and_vice_versa(tmp_path: Path) -> None:
    """A command registered in TclBridge but missing from COMMAND_HELP silently
    reports "unknown command" from `help <name>` even though it works fine --
    e.g. `retarget` shipped with no help entry for a while. Guard both
    directions so this can't regress silently again."""
    bridge = _bridge(tmp_path)
    registered = set(bridge._handlers.keys())
    documented = set(COMMAND_HELP.keys())

    assert registered - documented == set(), "registered command(s) with no help"
    assert documented - registered == set(), "help entry/entries for unknown command(s)"


def test_help_retarget_is_documented(tmp_path: Path) -> None:
    text = str(_bridge(tmp_path).call("help", "retarget"))

    assert text.startswith("retarget")
    assert "-patterns PATH -soc_access PATH -block NAME -o PATH" in text
    assert "Requires:" in text
    assert "Example:" in text


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


def test_help_glob_lists_matching_commands(tmp_path: Path) -> None:
    text = str(_bridge(tmp_path).call("help", "re*"))

    assert "report" in text
    assert "reset" in text
    assert "read_netlist" in text
    assert "use_lib_cells" not in text  # does not start with "re"


def test_help_glob_star_lists_all_commands(tmp_path: Path) -> None:
    text = str(_bridge(tmp_path).call("help", "*"))

    assert "read_netlist" in text
    assert "synth" in text
    assert "quit" in text


def test_help_glob_no_match_errors(tmp_path: Path) -> None:
    with pytest.raises(ShellError) as exc:
        _bridge(tmp_path).call("help", "zz*")

    assert exc.value.code == ("FAULTFLOW", "CONFIG", "INVALID_OPTION")
    assert "no commands match" in str(exc.value)
