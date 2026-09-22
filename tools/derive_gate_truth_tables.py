"""One-off derivation/verification tool (not imported by any production code,
mirrors the existing tools/derive_collapsing_rules.py precedent): cross-checks
gate_eval.cpp's hand-written closed-form formulas against the PDK vendor's own
Liberty `function` strings for each compound cell, as an independently-sourced
ground truth. Used to derive the hand-typed C++ lambdas added to
tests/cpp/atpg/test_cnf_truth_tables.cpp -- NOT itself part of the test suite,
and NOT a Liberty parser added to the C++ core (CLAUDE.md forbids that; this
stays a repo-root Python tool, never imported by faultflow/ or src/core/).

Run: python3 tools/derive_gate_truth_tables.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKY130_JSON = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
SKY130_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
OSU_JSON = ROOT / "cells/osu/osu035.json"
OSU_LIB = ROOT / "cells/osu/osu035_stdcells.lib"


# --- gate_eval.cpp ported to Python scalar bool, for the 63 target gates ---
# (transcribed directly from src/core/sim/gate_eval.cpp's eval_bitwise switch)
def pick(inputs: list[bool], i: int) -> bool:
    return inputs[i] if i < len(inputs) else False


def eval_gate_eval_cpp(gate: str, inputs: list[bool]) -> bool:
    i0 = pick(inputs, 0)
    i1 = pick(inputs, 1)
    i2 = pick(inputs, 2)
    i3 = pick(inputs, 3)
    i4 = pick(inputs, 4)
    i5 = pick(inputs, 5)
    f = {
        "A2111O": lambda: (i0 & i1) | i2 | i3 | i4,
        "A211O": lambda: (i0 & i1) | i2 | i3,
        "A21BO": lambda: (i0 & i1) | (not i2),
        "A21BOI": lambda: not ((i0 & i1) | (not i2)),
        "A21O": lambda: (i0 & i1) | i2,
        "A221O": lambda: (i0 & i1) | (i2 & i3) | i4,
        "A221OI": lambda: not ((i0 & i1) | (i2 & i3) | i4),
        "A222OI": lambda: not ((i0 & i1) | (i2 & i3) | (i4 & i5)),
        "A2BB2O": lambda: ((not i0) & (not i1)) | (i2 & i3),
        "A2BB2OI": lambda: (i0 | i1) & (not (i2 & i3)),
        "A311O": lambda: (i0 & i1 & i2) | i3 | i4,
        "A311OI": lambda: not ((i0 & i1 & i2) | i3 | i4),
        "A31O": lambda: (i0 & i1 & i2) | i3,
        "A31OI": lambda: not ((i0 & i1 & i2) | i3),
        "A32O": lambda: (i0 & i1 & i2) | (i3 & i4),
        "A32OI": lambda: not ((i0 & i1 & i2) | (i3 & i4)),
        "A41O": lambda: (i0 & i1 & i2 & i3) | i4,
        "A41OI": lambda: not ((i0 & i1 & i2 & i3) | i4),
        "ADDF_S": lambda: i0 ^ i1 ^ i2,
        "ADDF_CO": lambda: (i0 & i1) | (i1 & i2) | (i0 & i2),
        "ADDH_S": lambda: i0 ^ i1,
        "ADDH_CO": lambda: i0 & i1,
        "AND3": lambda: i0 & i1 & i2,
        "AND3B": lambda: (not i0) & i1 & i2,
        "AND4": lambda: i0 & i1 & i2 & i3,
        "AND4B": lambda: (not i0) & i1 & i2 & i3,
        "AND4BB": lambda: (not i0) & (not i1) & i2 & i3,
        "AOI21": lambda: not ((i0 & i1) | i2),
        "AOI22": lambda: not ((i0 & i1) | (i2 & i3)),
        "MUX2": lambda: not ((i2 & i0) | ((not i2) & i1)),
        "MUX2I": lambda: not (((not i2) & i0) | (i2 & i1)),
        "MUX4": lambda: ((not i4) & (not i5) & i0)
        | (i4 & (not i5) & i1)
        | ((not i4) & i5 & i2)
        | (i4 & i5 & i3),
        "NAND2B": lambda: i0 | (not i1),
        "NAND3": lambda: not (i0 & i1 & i2),
        "NAND4": lambda: not (i0 & i1 & i2 & i3),
        "NAND4B": lambda: i0 | (not i1) | (not i2) | (not i3),
        "NAND4BB": lambda: i0 | i1 | (not i2) | (not i3),
        "NOR3": lambda: not (i0 | i1 | i2),
        "NOR4": lambda: not (i0 | i1 | i2 | i3),
        "NOR4BB": lambda: (not i0) & (not i1) & i2 & i3,
        "O2111A": lambda: (i0 | i1) & i2 & i3 & i4,
        "O2111AI": lambda: not ((i0 | i1) & i2 & i3 & i4),
        "O211A": lambda: (i0 | i1) & i2 & i3,
        "O21BA": lambda: (i0 | i1) & (not i2),
        "O21BAI": lambda: not ((i0 | i1) & (not i2)),
        "O221A": lambda: (i0 | i1) & (i2 | i3) & i4,
        "O221AI": lambda: not ((i0 | i1) & (i2 | i3) & i4),
        "O22A": lambda: (i0 | i1) & (i2 | i3),
        "O22AI": lambda: not ((i0 | i1) & (i2 | i3)),
        "O2BB2A": lambda: (not (i0 & i1)) & (i2 | i3),
        "O2BB2AI": lambda: (i0 & i1) | (not (i2 | i3)),
        "O311A": lambda: (i0 | i1 | i2) & i3 & i4,
        "O31A": lambda: (i0 | i1 | i2) & i3,
        "O32A": lambda: (i0 | i1 | i2) & (i3 | i4),
        "O41A": lambda: (i0 | i1 | i2 | i3) & i4,
        "O41AI": lambda: not ((i0 | i1 | i2 | i3) & i4),
        "OAI21": lambda: not ((i0 | i1) & i2),
        "OAI22": lambda: not ((i0 | i1) & (i2 | i3)),
        "OR2B": lambda: i0 | (not i1),
        "OR3": lambda: i0 | i1 | i2,
        "OR4": lambda: i0 | i1 | i2 | i3,
        "OR4BB": lambda: i0 | i1 | (not i2) | (not i3),
        "XOR3": lambda: i0 ^ i1 ^ i2,
    }
    return f[gate]()


TARGET_GATES = [
    "A2111O",
    "A211O",
    "A21BO",
    "A21BOI",
    "A21O",
    "A221O",
    "A221OI",
    "A222OI",
    "A2BB2O",
    "A2BB2OI",
    "A311O",
    "A311OI",
    "A31O",
    "A31OI",
    "A32O",
    "A32OI",
    "A41O",
    "A41OI",
    "ADDF_CO",
    "ADDF_S",
    "ADDH_CO",
    "ADDH_S",
    "AND3",
    "AND3B",
    "AND4",
    "AND4B",
    "AND4BB",
    "AOI21",
    "AOI22",
    "MUX2",
    "MUX2I",
    "MUX4",
    "NAND2B",
    "NAND3",
    "NAND4",
    "NAND4B",
    "NAND4BB",
    "NOR3",
    "NOR4",
    "NOR4BB",
    "O2111A",
    "O2111AI",
    "O211A",
    "O21BA",
    "O21BAI",
    "O221A",
    "O221AI",
    "O22A",
    "O22AI",
    "O2BB2A",
    "O2BB2AI",
    "O311A",
    "O31A",
    "O32A",
    "O41A",
    "O41AI",
    "OAI21",
    "OAI22",
    "OR2B",
    "OR3",
    "OR4",
    "OR4BB",
    "XOR3",
]


def load_cellmap(path: Path) -> dict:
    return json.loads(path.read_text())


def find_entry(cellmap: dict, gate: str):
    for pattern, entry in cellmap.items():
        if entry.get("gate_type") == gate:
            return pattern, entry
    return None, None


def concrete_liberty_names(pattern: str) -> list[str]:
    base = pattern.replace("*", "")
    # sky130 drive strengths and OSU X-strengths
    return [base + s for s in ("1", "2", "4", "6", "8", "X1", "X2", "X4")] + [
        pattern.replace("_*", "")
    ]


def extract_liberty_functions(
    lib_text: str, cell_names: list[str], pin_names: list[str]
) -> dict | None:
    for name in cell_names:
        # sky130 style: cell ("name") { ... }
        m = re.search(r'cell\s*\(\s*"?' + re.escape(name) + r'"?\s*\)\s*\{', lib_text)
        if not m:
            continue
        start = m.end()
        depth = 1
        i = start
        while depth > 0 and i < len(lib_text):
            if lib_text[i] == "{":
                depth += 1
            elif lib_text[i] == "}":
                depth -= 1
            i += 1
        block = lib_text[start:i]
        funcs = {}
        for pin in pin_names:
            pm = re.search(r'pin\s*\(\s*"?' + re.escape(pin) + r'"?\s*\)\s*\{', block)
            if not pm:
                continue
            pstart = pm.end()
            pdepth = 1
            j = pstart
            while pdepth > 0 and j < len(block):
                if block[j] == "{":
                    pdepth += 1
                elif block[j] == "}":
                    pdepth -= 1
                j += 1
            pblock = block[pstart:j]
            fm = re.search(r'function\s*:\s*"([^"]*)"', pblock)
            if fm:
                funcs[pin] = fm.group(1)
        if funcs:
            return funcs
    return None


def tokenize_liberty(expr: str, osu_style: bool) -> list[tuple]:
    """Tokenize a Liberty boolean function string. Both dialects use '!' for
    NOT and parens for grouping; OSU uses '+' for OR and juxtaposition (bare
    whitespace between two operands) for AND; Sky130 uses explicit '&'/'|'."""
    tokens: list[tuple] = []
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(("LP",))
            i += 1
        elif c == ")":
            tokens.append(("RP",))
            i += 1
        elif c == "!":
            tokens.append(("NOT",))
            i += 1
        elif c == "&":
            tokens.append(("AND",))
            i += 1
        elif c == "|":
            tokens.append(("OR",))
            i += 1
        elif c == "^":
            tokens.append(("XOR",))
            i += 1
        elif osu_style and c == "+":
            tokens.append(("OR",))
            i += 1
        elif c.isalnum() or c == "_":
            j = i
            while j < n and (expr[j].isalnum() or expr[j] == "_"):
                j += 1
            tokens.append(("ID", expr[i:j]))
            i = j
        else:
            raise ValueError(f"unexpected character {c!r} in {expr!r}")
    return tokens


class LibertyExprParser:
    """Recursive-descent evaluator (NOT > AND > OR precedence), with
    juxtaposition (two atoms adjacent with no explicit operator) treated as
    an implicit AND -- evaluates directly against a bit vector rather than
    generating Python source text, so there is no risk of Python's `not`
    binding more loosely than `&`/`|` silently mis-grouping the formula."""

    ATOM_START = {"ID", "LP", "NOT"}

    def __init__(
        self, tokens: list[tuple], pin_to_idx: dict[str, int], bits: list[bool]
    ):
        self.tokens = tokens
        self.pos = 0
        self.pin_to_idx = pin_to_idx
        self.bits = bits

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse_or(self) -> bool:
        left = self.parse_and()
        while self.peek() is not None and self.peek()[0] == "OR":
            self.advance()
            right = self.parse_and()
            left = left or right
        return left

    def parse_and(self) -> bool:
        left = self.parse_not()
        while True:
            tok = self.peek()
            if tok is not None and tok[0] == "AND":
                self.advance()
                right = self.parse_not()
                left = left and right
            elif tok is not None and tok[0] in self.ATOM_START:
                right = self.parse_not()
                left = left and right
            else:
                break
        return left

    def parse_not(self) -> bool:
        if self.peek() is not None and self.peek()[0] == "NOT":
            self.advance()
            return not self.parse_not()
        return self.parse_atom()

    def parse_atom(self) -> bool:
        tok = self.advance()
        if tok[0] == "LP":
            val = self.parse_or()
            close = self.advance()
            if close[0] != "RP":
                raise ValueError("expected closing paren")
            return val
        if tok[0] == "ID":
            idx = self.pin_to_idx[tok[1]]
            return self.bits[idx]
        raise ValueError(f"unexpected token {tok!r}")

    def parse(self) -> bool:
        val = self.parse_or()
        if self.pos != len(self.tokens):
            raise ValueError(f"trailing tokens after parse: {self.tokens[self.pos:]}")
        return val


def eval_liberty_expr(
    expr: str, pin_to_idx: dict[str, int], osu_style: bool, bits: list[bool]
) -> bool:
    tokens = tokenize_liberty(expr, osu_style)
    return LibertyExprParser(tokens, pin_to_idx, bits).parse()


def main():
    sky_cellmap = load_cellmap(SKY130_JSON)
    osu_cellmap = load_cellmap(OSU_JSON)
    sky_lib = SKY130_LIB.read_text(errors="ignore")
    osu_lib = OSU_LIB.read_text(errors="ignore")

    results = []
    for gate in TARGET_GATES:
        # ADDF_S/ADDF_CO/ADDH_S/ADDH_CO are pre-split from ADDF/ADDH.
        lookup_gate = {
            "ADDF_S": "ADDF",
            "ADDF_CO": "ADDF",
            "ADDH_S": "ADDH",
            "ADDH_CO": "ADDH",
        }.get(gate, gate)
        pattern, entry = find_entry(sky_cellmap, lookup_gate)
        lib_text, osu_style = sky_lib, False
        if entry is None:
            pattern, entry = find_entry(osu_cellmap, lookup_gate)
            lib_text, osu_style = osu_lib, True
        if entry is None:
            results.append((gate, "NO_CELLMAP_ENTRY", None, None))
            continue
        inputs = entry["inputs"]
        outputs = entry["outputs"]
        pin_to_idx = {p: idx for idx, p in enumerate(inputs)}
        names = concrete_liberty_names(pattern)
        if gate == "ADDF_S":
            out_pin = "SUM" if not osu_style else None
        elif gate == "ADDF_CO":
            out_pin = "COUT" if not osu_style else None
        elif gate == "ADDH_S":
            out_pin = "SUM" if not osu_style else None
        elif gate == "ADDH_CO":
            out_pin = "COUT" if not osu_style else None
        else:
            # single-output cell: take the sole outputs dict value
            out_pin = list(outputs.values())[0]
        funcs = extract_liberty_functions(
            lib_text, names, [out_pin] if out_pin else list(outputs.values())
        )
        if not funcs or out_pin not in funcs:
            results.append((gate, "NO_LIBERTY_FUNCTION", pattern, None))
            continue
        raw_expr = funcs[out_pin]
        arity = len(inputs)
        mismatch_at = None
        for mask in range(1 << arity):
            bits = [bool((mask >> k) & 1) for k in range(arity)]
            try:
                lib_val = eval_liberty_expr(raw_expr, pin_to_idx, osu_style, bits)
            except Exception as exc:
                results.append(
                    (gate, f"PARSE_ERROR: {exc} :: {raw_expr!r}", pattern, raw_expr)
                )
                mismatch_at = "PARSE_ERROR"
                break
            impl_val = eval_gate_eval_cpp(gate, bits)
            if lib_val != impl_val:
                mismatch_at = bits
                break
        if mismatch_at == "PARSE_ERROR":
            continue
        elif mismatch_at is not None:
            results.append((gate, f"MISMATCH at {mismatch_at}", pattern, raw_expr))
        else:
            results.append((gate, "MATCH", pattern, raw_expr))

    ok = [r for r in results if r[1] == "MATCH"]
    bad = [r for r in results if r[1] != "MATCH"]
    print(f"MATCH: {len(ok)}/{len(results)}")
    print()
    for gate, status, pattern, expr in results:
        marker = "OK  " if status == "MATCH" else "FAIL"
        print(f"{marker} {gate:10s} {status:30s} {pattern} {expr!r}")
    if bad:
        raise SystemExit(f"{len(bad)} gate(s) did not match Liberty ground truth")


if __name__ == "__main__":
    main()
