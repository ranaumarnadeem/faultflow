from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from faultflow.shell.lineedit import _CommandCompleter, enable_line_editing

readline = pytest.importorskip("readline")


def test_enable_line_editing_returns_true_and_reads_history(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    history.write_text("report\nreset\n")
    assert (
        enable_line_editing(commands=["report", "reset"], history_path=history) is True
    )


def test_enable_line_editing_tolerates_missing_history(tmp_path: Path) -> None:
    # A non-existent history file must not raise.
    assert enable_line_editing(history_path=tmp_path / "nope") is True


def test_enable_line_editing_degrades_without_readline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "readline":
            raise ImportError("no readline")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert (
        enable_line_editing(commands=["report"], history_path=tmp_path / "h") is False
    )


def test_completer_completes_first_word(monkeypatch: pytest.MonkeyPatch) -> None:
    completer = _CommandCompleter(["report", "reset", "resume", "synth", "help"])
    monkeypatch.setattr(readline, "get_begidx", lambda: 0)
    monkeypatch.setattr(readline, "get_line_buffer", lambda: "re")

    assert completer.complete("re", 0) == "report"
    assert completer.complete("re", 1) == "reset"
    assert completer.complete("re", 2) == "resume"
    assert completer.complete("re", 3) is None


def test_completer_completes_help_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    completer = _CommandCompleter(["report", "reset", "synth", "help"])
    monkeypatch.setattr(readline, "get_begidx", lambda: 5)
    monkeypatch.setattr(readline, "get_line_buffer", lambda: "help re")

    assert completer.complete("re", 0) == "report"
    assert completer.complete("re", 1) == "reset"


def test_completer_skips_non_command_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completer = _CommandCompleter(["report", "reset", "synth"])
    # Second token of a non-help command (e.g. a path arg) → no completion.
    monkeypatch.setattr(readline, "get_begidx", lambda: 6)
    monkeypatch.setattr(readline, "get_line_buffer", lambda: "synth re")

    assert completer.complete("re", 0) is None
