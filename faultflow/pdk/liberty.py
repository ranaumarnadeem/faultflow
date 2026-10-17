"""A Liberty reader for what FaultFlow derives from a cell library: groups (``library``,
``cell``, ``pin``, ``ff``, ``latch``, ``test_cell``, ...), their arguments and simple
attributes. Complex attributes (``values ("...")``, ``index_1 (...)``) are read and
dropped; timing and power groups parse like any other group.

The grammar is the Liberty one: a statement is ``name : value ;`` (a simple attribute),
``name ( args ) ;`` (a complex attribute) or ``name ( args ) { statements }`` (a group).
Comments (``/* */`` and ``//``) and backslash line continuations are skipped."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator


class LibertyError(RuntimeError):
    """A Liberty file (or expression) FaultFlow cannot read."""


@dataclass
class Group:
    name: str
    args: list[str]
    attrs: dict[str, str] = field(default_factory=dict)
    groups: list["Group"] = field(default_factory=list)

    def all(self, name: str) -> list["Group"]:
        """The direct subgroups called ``name``, in file order."""
        return [g for g in self.groups if g.name == name]

    def find(self, name: str) -> "Group | None":
        """The first direct subgroup called ``name``, or None."""
        for g in self.groups:
            if g.name == name:
                return g
        return None


_TOKEN = re.compile(
    r"\s+|/\*.*?\*/|//[^\n]*|\\\r?\n"  # skipped
    r'|("(?:[^"\\]|\\.)*")'  # 1: string
    r"|([{}();:,])"  # 2: punctuation
    r'|([^\s{}();:,"]+)',  # 3: word
    re.S,
)


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if m is None:
            raise LibertyError(
                f"unreadable text at offset {pos}: {text[pos:pos + 20]!r}"
            )
        pos = m.end()
        if m.group(1) is not None:
            yield "str", m.group(1)[1:-1].replace("\\\n", "").replace("\\\r\n", "")
        elif m.group(2) is not None:
            yield m.group(2), m.group(2)
        elif m.group(3) is not None:
            yield "word", m.group(3)


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = list(_tokens(text))
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise LibertyError("unexpected end of file")
        if kind is not None and tok[0] != kind:
            raise LibertyError(f"expected {kind!r}, found {tok[1]!r}")
        self.i += 1
        return tok

    def value(self) -> str:
        kind, text = self.take()
        if kind not in ("str", "word"):
            raise LibertyError(f"expected a value, found {text!r}")
        return text

    def args(self) -> list[str]:
        self.take("(")
        out: list[str] = []
        while True:
            tok = self.peek()
            if tok is None:
                raise LibertyError("unexpected end of file in arguments")
            if tok[0] == ")":
                self.take()
                return out
            if tok[0] == ",":
                self.take()
                continue
            out.append(self.value())

    def statements(self, into: Group, closing: bool) -> None:
        while True:
            tok = self.peek()
            if tok is None:
                if closing:
                    raise LibertyError(f"group {into.name!r} is not closed")
                return
            if tok[0] == "}":
                if not closing:
                    raise LibertyError("unbalanced '}'")
                self.take()
                return
            if tok[0] == ";":
                self.take()
                continue
            name = self.take("word")[1]
            nxt = self.peek()
            if nxt is not None and nxt[0] == ":":
                self.take()
                into.attrs[name] = self.value()
                # A value may run on (e.g. an expression without quotes): keep words.
                while (p := self.peek()) is not None and p[0] in ("word", "str"):
                    into.attrs[name] += " " + self.take()[1]
                continue
            if nxt is not None and nxt[0] == "(":
                args = self.args()
                after = self.peek()
                if after is not None and after[0] == "{":
                    self.take()
                    group = Group(name, args)
                    self.statements(group, closing=True)
                    into.groups.append(group)
                # else a complex attribute: dropped
                continue
            raise LibertyError(
                f"unexpected {nxt[1] if nxt else 'end'!r} after {name!r}"
            )


def parse_liberty(text: str) -> Group:
    """The file's top group (normally ``library``)."""
    parser = _Parser(text)
    root = Group("<file>", [])
    parser.statements(root, closing=False)
    if len(root.groups) != 1:
        raise LibertyError(f"expected one top-level group, found {len(root.groups)}")
    return root.groups[0]
