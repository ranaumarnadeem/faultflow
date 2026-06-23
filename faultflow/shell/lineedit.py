from __future__ import annotations

import atexit
from collections.abc import Iterable
from pathlib import Path

_DEFAULT_HISTORY = Path.home() / ".faultflow_history"
_HISTORY_LIMIT = 1000


class _CommandCompleter:
    """Tab-completer for the shell.

    Completes the command name (the first word on the line) and the argument of
    ``help`` (which takes a command name or glob). Any other token position
    yields no completion, so file/option arguments are left untouched.
    """

    def __init__(self, commands: Iterable[str]) -> None:
        self._commands = sorted(set(commands))
        self._matches: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        import readline

        if state == 0:
            begin = readline.get_begidx()
            prefix = readline.get_line_buffer()[:begin].split()
            first_word = begin == 0
            help_arg = len(prefix) == 1 and prefix[0] == "help"
            self._matches = (
                [name for name in self._commands if name.startswith(text)]
                if first_word or help_arg
                else []
            )
        return self._matches[state] if state < len(self._matches) else None


def enable_line_editing(
    commands: Iterable[str] | None = None,
    history_path: Path | None = None,
) -> bool:
    """Enable arrow-key history, cursor editing and Tab-completion for input().

    Importing ``readline`` registers it as the line editor the builtin
    ``input()`` uses, which is what provides Up/Down history recall and
    Left/Right cursor movement (without it the terminal emits raw escape
    sequences such as ``^[[A``). Returns ``True`` if a readline backend was
    activated, ``False`` if none is available (e.g. native Windows without
    ``pyreadline3``) — in which case the shell behaves exactly as before.
    """
    try:
        import readline
    except ImportError:
        return False

    path = history_path or _DEFAULT_HISTORY
    try:
        readline.read_history_file(str(path))
    except OSError:
        pass  # no history yet, or unreadable — start fresh
    readline.set_history_length(_HISTORY_LIMIT)

    def _save_history() -> None:
        try:
            readline.write_history_file(str(path))
        except OSError:
            pass

    atexit.register(_save_history)

    if commands is not None:
        readline.set_completer(_CommandCompleter(commands).complete)
        # GNU readline uses "tab: complete"; libedit (some macOS builds) differs.
        bind = (
            "bind ^I rl_complete"
            if "libedit" in (readline.__doc__ or "")
            else "tab: complete"
        )
        readline.parse_and_bind(bind)

    return True
