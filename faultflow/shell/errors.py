from __future__ import annotations


class ShellError(RuntimeError):
    def __init__(self, message: str, *code: str) -> None:
        super().__init__(message)
        self.code = ("FAULTFLOW", *code)


def precondition(message: str, code: str) -> ShellError:
    return ShellError(message, "PRECONDITION", code)


def unsupported(message: str, code: str) -> ShellError:
    return ShellError(message, "UNSUPPORTED", code)
