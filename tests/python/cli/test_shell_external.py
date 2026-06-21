from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.shell.repl import run_shell


def _interactive(monkeypatch: pytest.MonkeyPatch, *commands: str) -> None:
    inputs = iter([*commands, "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))


def test_interactive_shell_runs_linux_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "visible.txt").write_text("data", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _interactive(monkeypatch, "ls")

    assert run_shell(output_root=tmp_path / "output") == 0

    captured = capsys.readouterr()
    assert "visible.txt" in captured.out
    assert "invalid command name" not in captured.err


def test_interactive_external_command_preserves_quoted_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "space name.txt"
    path.write_text("quoted content\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _interactive(monkeypatch, 'cat "space name.txt"')

    assert run_shell(output_root=tmp_path / "output") == 0

    assert "quoted content" in capsys.readouterr().out


def test_tcl_commands_keep_precedence_over_external_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _interactive(monkeypatch, "pwd")

    assert run_shell(output_root=tmp_path / "output") == 0

    assert str(tmp_path) in capsys.readouterr().out


def test_unknown_command_remains_a_tcl_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _interactive(monkeypatch, "definitely_not_a_faultflow_command")

    assert run_shell(output_root=tmp_path / "output") == 0

    captured = capsys.readouterr()
    assert 'invalid command name "definitely_not_a_faultflow_command"' in captured.err


def test_external_nonzero_exit_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _interactive(monkeypatch, 'sh -c "echo failed >&2; exit 7"')

    assert run_shell(output_root=tmp_path / "output") == 0

    captured = capsys.readouterr()
    assert "failed" in captured.err
    assert "Command exited with status 7." in captured.err


def test_script_mode_does_not_implicitly_run_external_commands(
    tmp_path: Path,
) -> None:
    script = tmp_path / "external.tcl"
    script.write_text("ls\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["shell", "-f", str(script), "--out", str(tmp_path / "output")])

    assert exc.value.code == 2
