"""Expand the STIL faultflow.scan.stil writes back into tester cycles, by STIL's own
rules: every signal keeps its last waveform character; a condition statement (C)
sets characters without a cycle; each vector (V) is one cycle; Loop repeats; a
procedure call runs the procedure with its arguments, each '#' taking the next
character of each of its signals' data, the Shift repeating until the data is used
up.

Readers differ on the signal states a procedure starts from and leaves. As a
subroutine with its own states, it starts with every signal undefined and its
caller's states return after it; run like a macro, it starts from its caller's and
leaves its own. expand takes either (`inherit`), and the writer's STIL must give
the same cycles under both.

The waveform characters are the writer's: an input's 0 and 1, a clock's 0 and P
(a pulse after the strobe), an output's L, H and X (not compared). The expanded
cycles must be tester_program.cycles' -- the cycles the real-cell replay checks --
which is what makes the STIL right. Data an independent parser (Semi-ATE-STIL)
lets through is checked here: every character is one its signal can take, and a
Shift's data is equally long for every signal."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:(\{\*.*?\*\})|("[^"]*")|('[^']*')|([{};=:])|([^\s{};=:"']+))""",
    re.S,
)


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"STIL: can't read {text[position:position + 40]!r}")
        position = match.end()
        annotation, *rest = match.groups()
        if annotation is None:
            found.append(next(token for token in rest if token is not None))
        else:
            found.append(";")  # Ann {* ... *} is a whole statement
    return found


@dataclass
class _Statement:
    words: list[str]
    body: list["_Statement"] | None = None


def _statements(tokens: list[str], start: int = 0) -> tuple[list[_Statement], int]:
    """The statements from `start` to the closing brace (or the end)."""
    statements: list[_Statement] = []
    words: list[str] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "}":
            if words:
                raise ValueError(f"STIL: unterminated statement {words}")
            return statements, index
        if token == ";":
            statements.append(_Statement(words))
            words = []
        elif token == ":":
            statements.append(_Statement(words + [":"]))  # a label
            words = []
        elif token == "{":
            body, index = _statements(tokens, index)
            statements.append(_Statement(words, body))
            words = []
        else:
            words.append(token)
    if words:
        raise ValueError(f"STIL: unterminated statement {words}")
    return statements, index


def _name(token: str) -> str:
    return token[1:-1] if token.startswith('"') else token


@dataclass(frozen=True)
class ExpandedCycle:
    inputs: dict[str, int]
    expect: dict[str, int]
    pulse: bool


def expand(text: str, *, inherit: bool = False) -> list[ExpandedCycle]:
    """The cycles the STIL applies, in order. With `inherit` a procedure starts
    from its caller's signal states and leaves its own, as a macro does; without
    it, a procedure starts with every signal undefined, and its caller's states
    return after it."""
    top, _ = _statements(_tokens(text))
    blocks = {s.words[0]: s for s in top if s.body is not None}
    direction: dict[str, str] = {}
    for statement in blocks["Signals"].body or []:
        direction[_name(statement.words[0])] = statement.words[1]
    groups: dict[str, list[str]] = {}
    for statement in blocks["SignalGroups"].body or []:
        name, _, expression = statement.words
        groups[_name(name)] = re.findall(r'"([^"]*)"', expression)
    clocks = set(groups.get("_clk", []))
    procedures = {
        _name(s.words[0]): s.body or [] for s in blocks["Procedures"].body or []
    }
    pattern = next(s for s in top if s.words and s.words[0] == "Pattern")
    state: dict[str, str | None] = dict.fromkeys(direction)
    cycles: list[ExpandedCycle] = []

    def members(target: str) -> list[str]:
        return groups.get(target, [target])

    def assign(target: str, data: str, streams: dict[str, list[str]]) -> None:
        signals = members(target)
        if len(data) != len(signals):
            raise ValueError(f"STIL: {target} = {data}: {len(signals)} signals")
        if set(data) == {"#"}:
            if not all(streams.get(signal) for signal in signals):
                raise ValueError(f"STIL: {target} = {data}: no data passed")
            data = "".join(streams[signal].pop(0) for signal in signals)
        for signal, character in zip(signals, data):
            allowed = (
                "0P"
                if signal in clocks
                else "01" if direction[signal] == "In" else "LHX"
            )
            if character not in allowed:
                raise ValueError(f"STIL: {signal} can't take {character!r}")
            state[signal] = character

    def vector(statement: _Statement, streams: dict[str, list[str]]) -> None:
        for assignment in statement.body or []:
            target, _, data = assignment.words
            assign(_name(target), data, streams)
        undefined = [s for s, v in state.items() if v is None and direction[s] == "In"]
        if undefined:
            raise ValueError(f"STIL: a vector with inputs undefined: {undefined}")
        cycles.append(
            ExpandedCycle(
                inputs={
                    s: int(str(v))
                    for s, v in state.items()
                    if direction[s] == "In" and s not in clocks
                },
                expect={
                    s: int(v == "H")
                    for s, v in state.items()
                    if direction[s] == "Out" and v in ("L", "H")
                },
                pulse=any(state[clock] == "P" for clock in clocks),
            )
        )

    def arguments(statement: _Statement) -> dict[str, list[str]]:
        """A call's data, per signal: a group's is its signals' in turn."""
        passed: dict[str, list[str]] = {}
        for assignment in statement.body or []:
            target, _, data = assignment.words
            signals = members(_name(target))
            if len(data) % len(signals):
                raise ValueError(f"STIL: {target} = {data}: {len(signals)} signals")
            for k, signal in enumerate(signals):
                if signal in passed:
                    raise ValueError(f"STIL: a call passes {signal} twice")
                passed[signal] = list(data[k :: len(signals)])
        return passed

    def run(statements: list[_Statement], streams: dict[str, list[str]]) -> None:
        for statement in statements:
            words = statement.words
            keyword = words[0] if words else ""
            if words and words[-1] == ":" or keyword == "W":
                continue
            if keyword == "C":
                for assignment in statement.body or []:
                    target, _, data = assignment.words
                    assign(_name(target), data, streams)
            elif keyword == "V":
                vector(statement, streams)
            elif keyword == "Loop":
                for _ in range(int(words[1])):
                    run(statement.body or [], streams)
            elif keyword == "Shift":
                left = {len(stream) for stream in streams.values() if stream}
                if len(left) > 1:
                    raise ValueError(f"STIL: Shift data of lengths {sorted(left)}")
                for _ in range(left.pop() if left else 0):
                    run(statement.body or [], streams)
            elif keyword == "Call":
                passed = arguments(statement)
                caller = dict(state)
                if not inherit:
                    state.update(dict.fromkeys(state))
                run(procedures[_name(words[1])], passed)
                if any(passed.values()):
                    raise ValueError("STIL: a call passes data its procedure leaves")
                if not inherit:
                    state.update(caller)
            else:
                raise ValueError(f"STIL: statement {words} isn't expanded here")

    run(pattern.body or [], {})
    return cycles


def model(program: list[Any]) -> list[ExpandedCycle]:
    """tester_program cycles as an expansion gives them: the outputs compared."""
    return [
        ExpandedCycle(
            inputs=dict(cycle.inputs),
            expect={n: int(v) for n, v in cycle.expect.items() if v is not None},
            pulse=cycle.pulse,
        )
        for cycle in program
    ]
