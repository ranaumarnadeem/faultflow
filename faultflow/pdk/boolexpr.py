"""Liberty boolean expressions (a pin's ``function``, an ``ff`` group's ``next_state``):
``!`` and postfix ``'`` negate, ``^`` is XOR, ``&``, ``*`` or juxtaposition is AND and
``|`` or ``+`` is OR, in that order of precedence (highest first); ``0`` and ``1`` are
constants. Liberty's precedence puts XOR above AND."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from faultflow.pdk.liberty import LibertyError

_TOK = re.compile(r"\s*(?:([A-Za-z_][A-Za-z0-9_\[\].]*)|([01])|([!'^&*|+()]))")


@dataclass(frozen=True)
class Expr:
    op: str  # "var", "const", "not", "and", "or", "xor"
    args: tuple["Expr", ...] = ()
    name: str = ""
    value: bool = False

    def eval(self, env: Mapping[str, bool]) -> bool:
        if self.op == "var":
            if self.name not in env:
                raise LibertyError(f"no value for {self.name!r}")
            return bool(env[self.name])
        if self.op == "const":
            return self.value
        if self.op == "not":
            return not self.args[0].eval(env)
        if self.op == "and":
            return all(a.eval(env) for a in self.args)
        if self.op == "or":
            return any(a.eval(env) for a in self.args)
        result = False
        for a in self.args:
            result ^= a.eval(env)
        return result

    def variables(self) -> set[str]:
        if self.op == "var":
            return {self.name}
        out: set[str] = set()
        for a in self.args:
            out |= a.variables()
        return out


def _lex(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOK.match(text, pos)
        if m is None or m.end() == pos:
            raise LibertyError(f"unreadable expression {text!r} at {pos}")
        pos = m.end()
        if m.group(1):
            toks.append(("var", m.group(1)))
        elif m.group(2):
            toks.append(("const", m.group(2)))
        elif m.group(3):
            toks.append((m.group(3), m.group(3)))
    return toks


class _P:
    def __init__(self, toks: list[tuple[str, str]], text: str) -> None:
        self.toks, self.i, self.text = toks, 0, text

    def peek(self) -> str | None:
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        if self.i >= len(self.toks):
            raise LibertyError(f"expression {self.text!r} ends early")
        self.i += 1
        return self.toks[self.i - 1]

    def parse_or(self) -> Expr:
        terms = [self.parse_and()]
        while self.peek() in ("|", "+"):
            self.take()
            terms.append(self.parse_and())
        return terms[0] if len(terms) == 1 else Expr("or", tuple(terms))

    def parse_and(self) -> Expr:
        terms = [self.parse_xor()]
        while True:
            nxt = self.peek()
            if nxt in ("&", "*"):
                self.take()
                terms.append(self.parse_xor())
            elif nxt in ("var", "const", "(", "!"):  # juxtaposition
                terms.append(self.parse_xor())
            else:
                break
        return terms[0] if len(terms) == 1 else Expr("and", tuple(terms))

    def parse_xor(self) -> Expr:
        terms = [self.parse_not()]
        while self.peek() == "^":
            self.take()
            terms.append(self.parse_not())
        return terms[0] if len(terms) == 1 else Expr("xor", tuple(terms))

    def parse_not(self) -> Expr:
        if self.peek() == "!":
            self.take()
            return Expr("not", (self.parse_not(),))
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        atom = self.parse_atom()
        while self.peek() == "'":
            self.take()
            atom = Expr("not", (atom,))
        return atom

    def parse_atom(self) -> Expr:
        kind, text = self.take()
        if kind == "var":
            return Expr("var", name=text)
        if kind == "const":
            return Expr("const", value=text == "1")
        if kind == "(":
            inner = self.parse_or()
            if self.peek() != ")":
                raise LibertyError(f"expression {self.text!r}: missing ')'")
            self.take()
            return inner
        raise LibertyError(f"expression {self.text!r}: unexpected {text!r}")


def parse_expr(text: str) -> Expr:
    toks = _lex(text)
    if not toks:
        raise LibertyError("empty expression")
    parser = _P(toks, text)
    expr = parser.parse_or()
    if parser.i != len(toks):
        raise LibertyError(f"expression {text!r}: unexpected {toks[parser.i][1]!r}")
    return expr


def truth_table(expr: Expr, order: Sequence[str]) -> int:
    """Bit i is the value at the assignment where variable order[k] is bit k of i."""
    table = 0
    for i in range(1 << len(order)):
        env = {name: bool((i >> k) & 1) for k, name in enumerate(order)}
        if expr.eval(env):
            table |= 1 << i
    return table
