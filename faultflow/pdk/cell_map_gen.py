"""A FaultFlow cell map generated from a standard-cell library's Liberty.

- A combinational cell's ``function`` is matched to one of the engine's gate types by
  exhaustive truth table over every order of its input pins. The core's own gate
  evaluation (``core.eval_gate``) is the oracle, so a match is exactly what the
  simulator computes. The match fixes the entry's ``inputs`` order.
- A flop is read from its ``ff`` group (``clocked_on``, ``next_state``, ``clear``,
  ``preset`` and their conflict value) and its ``test_cell`` (the pins marked
  ``test_scan_in`` / ``test_scan_enable``). ``next_state`` must be the engine's model --
  scan mux, then an optional enable that holds ``IQ``, then the data pin -- checked by
  truth table, or the cell is listed.
- Every other cell is listed, never guessed: latches, tristates, clock-gating cells,
  physical cells (no logic output), and functions no gate type has.

Drive strengths of one cell (``<name>_1``, ``<name>_4``) share a ``<name>_*`` pattern
when they model alike and the pattern matches no other cell of the library."""

from __future__ import annotations

import fnmatch
import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from faultflow.pdk.boolexpr import Expr, parse_expr, truth_table
from faultflow.pdk.liberty import Group, LibertyError

MAX_INPUTS = 6
# Gate types a combinational match never picks: the constants (a tie cell is listed).
_NOT_SINGLE = {"CONST0", "CONST1"}


@dataclass(frozen=True)
class CellReport:
    """A library cell the map does not model as a gate or a flop, and why."""

    cell: str
    # latch | icg | tristate | physical | tie | no_gate_type | multi_output | ff
    kind: str
    detail: str


@dataclass
class GenerationResult:
    cell_map: dict[str, Any]
    report: list[CellReport] = field(default_factory=list)


class _Oracle:
    """Each gate type's truth table over its own inputs, from the core."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.by_arity: dict[int, list[tuple[str, int]]] = {}
        for name in core.gate_type_names():
            if name in _NOT_SINGLE:
                continue
            arity, table = self._table(name)
            if arity:
                self.by_arity.setdefault(arity, []).append((name, table))
        self.and2 = self._table("AND2")[1]
        self.addf_co = self._table("ADDF_CO")[1]

    def _table(self, name: str) -> tuple[int, int]:
        n = MAX_INPUTS
        full = 0
        for i in range(1 << n):
            if self.core.eval_gate(name, [bool((i >> k) & 1) for k in range(n)]):
                full |= 1 << i
        used = [
            k
            for k in range(n)
            if any(
                ((full >> i) & 1) != ((full >> (i ^ (1 << k))) & 1)
                for i in range(1 << n)
            )
        ]
        arity = max(used) + 1 if used else 0
        if used != list(range(arity)):
            return 0, 0
        table = 0
        for i in range(1 << arity):
            if (full >> i) & 1:
                table |= 1 << i
        return arity, table


def _permuted_matches(
    gate_table: int, cell_table: int, n: int
) -> list[tuple[int, ...]]:
    """Every perm with gate input i = cell pin perm[i] making the gate the cell."""
    out = []
    for perm in itertools.permutations(range(n)):
        ok = True
        for a in range(1 << n):
            b = 0
            for i in range(n):
                if (a >> perm[i]) & 1:
                    b |= 1 << i
            if ((gate_table >> b) & 1) != ((cell_table >> a) & 1):
                ok = False
                break
        if ok:
            out.append(perm)
    return out


def _base(name: str) -> str:
    return re.sub(r"_\d+$", "", name)


def _short(name: str) -> str:
    return _base(name).split("__")[-1].lower()


def _pins(cell: Group) -> dict[str, Group]:
    return {str(p.args[0]): p for p in cell.all("pin") if p.args}


def _single_var(expr: Expr) -> tuple[str, bool] | None:
    """(pin, active_high) for ``PIN`` or ``!PIN``, else None."""
    if expr.op == "var":
        return expr.name, True
    if expr.op == "not" and expr.args[0].op == "var":
        return expr.args[0].name, False
    return None


def _same(
    f: Expr, g: Expr, order: list[str], fix: Mapping[str, bool] | None = None
) -> bool:
    fix = dict(fix or {})
    free = [v for v in order if v not in fix]
    for i in range(1 << len(free)):
        env = dict(fix)
        env.update({v: bool((i >> k) & 1) for k, v in enumerate(free)})
        if f.eval(env) != g.eval(env):
            return False
    return True


class _Generator:
    def __init__(self, core: Any) -> None:
        self.oracle = _Oracle(core)
        self.report: list[CellReport] = []

    def listed(self, cell: str, kind: str, detail: str) -> dict[str, Any]:
        self.report.append(CellReport(cell, kind, detail))
        return {"unsupported": True, "reason": detail}

    # ---------------------------------------------------------------- combinational
    def match(
        self, short: str, pins: list[str], expr: Expr
    ) -> tuple[str, list[str]] | None:
        n = len(pins)
        table = truth_table(expr, pins)
        candidates = sorted(
            self.oracle.by_arity.get(n, []),
            key=lambda gt: (gt[0].lower() != short, gt[0]),
        )
        for name, gate_table in candidates:
            perms = _permuted_matches(gate_table, table, n)
            if perms:
                return name, [pins[i] for i in perms[0]]
        return None

    def combinational(self, name: str, cell: Group) -> dict[str, Any]:
        pins = _pins(cell)
        inputs = [p for p, g in pins.items() if g.attrs.get("direction") == "input"]
        outputs = {
            p: g
            for p, g in pins.items()
            if g.attrs.get("direction") in ("output", "inout")
        }
        if any("three_state" in g.attrs for g in outputs.values()):
            self.report.append(CellReport(name, "tristate", "tristate output"))
            return {"node_type": "TBUF", "reason": "tristate output"}
        functions: dict[str, Expr] = {}
        for pin, group in outputs.items():
            if "function" not in group.attrs:
                return self.listed(
                    name, "physical", f"physical cell: output {pin} has no function"
                )
            try:
                functions[pin] = parse_expr(group.attrs["function"])
            except LibertyError as exc:
                return self.listed(name, "no_gate_type", f"no gate type: {exc}")
        if any(not f.variables() for f in functions.values()):
            return self.listed(name, "tie", "tie cell (constant output)")
        used = sorted(set().union(*(f.variables() for f in functions.values())))
        if not set(used) <= set(inputs):
            return self.listed(
                name, "no_gate_type", f"no gate type: function reads {used}"
            )
        order = [p for p in inputs if p in used]
        if len(order) > MAX_INPUTS:
            return self.listed(
                name, "no_gate_type", f"no gate type: {len(order)} inputs"
            )
        short = _short(name)
        if len(functions) == 1:
            ((out, expr),) = functions.items()
            got = self.match(short, order, expr)
            if got is None:
                return self.listed(
                    name,
                    "no_gate_type",
                    f"no gate type for {out} = {cell_fn(cell, out)}",
                )
            gate, ins = got
            return {
                "node_type": "GATE",
                "gate_type": gate,
                "inputs": ins,
                "outputs": {out: out},
            }
        if len(functions) == 2:
            adder = self.adder(order, functions)
            if adder is not None:
                return adder
        return self.listed(
            name,
            "multi_output",
            f"no gate type: {len(functions)} outputs {sorted(functions)}",
        )

    def adder(
        self, order: list[str], functions: dict[str, Expr]
    ) -> dict[str, Any] | None:
        n = len(order)
        recipes = {3: ("ADDF", self.oracle.addf_co), 2: ("ADDH", self.oracle.and2)}
        if n not in recipes:
            return None
        gate, co_table = recipes[n]
        s_table = dict(self.oracle.by_arity.get(n, [])).get(gate)
        if s_table is None:
            return None
        for s_pin, co_pin in itertools.permutations(functions, 2):
            s_perms = _permuted_matches(
                s_table, truth_table(functions[s_pin], order), n
            )
            co_perms = set(
                _permuted_matches(co_table, truth_table(functions[co_pin], order), n)
            )
            for perm in s_perms:
                if perm in co_perms:
                    return {
                        "node_type": "GATE",
                        "gate_type": gate,
                        "inputs": [order[i] for i in perm],
                        "outputs": {"S": s_pin, "CO": co_pin},
                    }
        return None

    # ---------------------------------------------------------------- flops
    def flop(self, name: str, cell: Group, ff: Group) -> dict[str, Any]:
        def fail(detail: str) -> dict[str, Any]:
            return self.listed(name, "ff", f"flop not modelled: {detail}")

        iq = str(ff.args[0]) if ff.args else "IQ"
        iqn = str(ff.args[1]) if len(ff.args) > 1 else "IQ_N"
        pins = _pins(cell)
        try:
            clocked = parse_expr(ff.attrs["clocked_on"])
            nxt = parse_expr(ff.attrs["next_state"])
            clear = parse_expr(ff.attrs["clear"]) if "clear" in ff.attrs else None
            preset = parse_expr(ff.attrs["preset"]) if "preset" in ff.attrs else None
        except (KeyError, LibertyError) as exc:
            return fail(f"ff group: {exc}")
        clock = _single_var(clocked)
        if clock is None:
            return fail(f"clocked_on {ff.attrs['clocked_on']!r}")
        meta: dict[str, Any] = {"clock": clock[0]}
        controls: dict[str, dict[str, Any]] = {}
        for key, expr, value in (("clear", clear, 0), ("preset", preset, 1)):
            if expr is None:
                continue
            ctl = _single_var(expr)
            if ctl is None:
                return fail(f"{key} {ff.attrs[key]!r}")
            controls[key] = {
                "pin": ctl[0],
                "level": "HIGH" if ctl[1] else "LOW",
                "value": value,
            }

        # Scan pins, from the test_cell.
        scan: dict[str, Any] | None = None
        test_cell = cell.find("test_cell")
        if test_cell is not None:
            si = se = None
            se_high = True
            for pin, group in _pins(test_cell).items():
                kind = group.attrs.get("signal_type", "")
                if kind == "test_scan_in":
                    si = pin
                elif kind == "test_scan_enable":
                    se, se_high = pin, True
                elif kind == "test_scan_enable_inverted":
                    se, se_high = pin, False
                elif kind == "test_scan_in_inverted":
                    return fail("inverted scan-in")
            if si is None or se is None:
                return fail("test_cell without a scan-in and a scan enable")
            scan = {
                "in": si,
                "enable": se,
                "enable_polarity": "HIGH" if se_high else "LOW",
            }

        variables = sorted(
            nxt.variables() | ({scan["in"], scan["enable"]} if scan else set())
        )
        if iqn in variables:
            return fail("next_state reads IQ_N")
        functional: dict[str, bool] = {}
        if scan is not None:
            se_on = scan["enable_polarity"] == "HIGH"
            # Scan enabled: the scan-in, whatever else.
            if not _same(
                nxt, parse_expr(scan["in"]), variables, {scan["enable"]: se_on}
            ):
                return fail("next_state with scan enabled is not the scan-in")
            functional = {scan["enable"]: not se_on}
        f_vars = [v for v in variables if v not in functional and v != iq]
        data: str | None = None
        enable: dict[str, str] | None = None
        # Does the functional next state depend on IQ (an enable that holds it)?
        depends_iq = iq in variables and not _equal_under(
            nxt, variables, functional, iq
        )
        if not depends_iq:
            for d in f_vars:
                if _same(nxt, parse_expr(d), variables, functional):
                    data = d
                    break
        else:
            for e, d in itertools.permutations(f_vars, 2):
                for high in (True, False):
                    hold = {**functional, e: not high}
                    load = {**functional, e: high}
                    if _same(nxt, parse_expr(iq), variables, hold) and _same(
                        nxt, parse_expr(d), variables, load
                    ):
                        data, enable = d, {"pin": e, "level": "HIGH" if high else "LOW"}
                        break
                if data is not None:
                    break
        if data is None:
            return fail(f"next_state {ff.attrs['next_state']!r}")
        q = [p for p, g in pins.items() if g.attrs.get("function", "").strip() == iq]
        qn = [p for p, g in pins.items() if g.attrs.get("function", "").strip() == iqn]
        others = [
            p
            for p, g in pins.items()
            if g.attrs.get("direction") == "output" and p not in q + qn
        ]
        if len(q) != 1 or len(qn) > 1 or others:
            return fail(f"outputs Q={q} QN={qn} other={others}")
        if enable is not None and "clear" in controls:
            return fail("an enable and a clear together")
        meta.update(
            {
                "data": data,
                "output": q[0],
                "trigger": "POSEDGE" if clock[1] else "NEGEDGE",
            }
        )
        meta.update(controls)
        if enable is not None:
            meta["enable"] = enable
        if scan is not None:
            meta["scan"] = scan
        if "clear" in controls and "preset" in controls:
            both = ff.attrs.get("clear_preset_var1", "L")
            if both not in ("L", "H"):
                return fail(f"clear_preset_var1 {both!r}")
            meta["clear_preset_conflict_value"] = 1 if both == "H" else 0
        inputs = [clock[0], data]
        if enable is not None:
            inputs.append(enable["pin"])
        if scan is not None:
            inputs += [scan["in"], scan["enable"]]
        inputs += [controls[k]["pin"] for k in ("clear", "preset") if k in controls]
        outputs = {"Q": q[0]}
        if qn:
            outputs["QN"] = qn[0]
        return {"node_type": "FF", "inputs": inputs, "outputs": outputs, "ff": meta}

    # ---------------------------------------------------------------- cells
    def entry(self, cell: Group) -> dict[str, Any]:
        name = str(cell.args[0])
        if "clock_gating_integrated_cell" in cell.attrs:
            return self.listed(name, "icg", "clock-gating cell (deferred)")
        if cell.find("latch") is not None:
            return self.listed(name, "latch", "latch (out of scope: no X-state)")
        ff = cell.find("ff")
        if ff is not None:
            return self.flop(name, cell, ff)
        if cell.find("statetable") is not None:
            return self.listed(
                name, "physical", "sequential cell described only by a statetable"
            )
        outputs = [
            p
            for p, g in _pins(cell).items()
            if g.attrs.get("direction") in ("output", "inout")
        ]
        if not outputs:
            return self.listed(name, "physical", "physical cell (no logic output)")
        return self.combinational(name, cell)


def _equal_under(
    nxt: Expr, variables: list[str], fix: Mapping[str, bool], iq: str
) -> bool:
    """Whether the next state is the same for IQ = 0 and IQ = 1 (given ``fix``)."""
    free = [v for v in variables if v not in fix and v != iq]
    for i in range(1 << len(free)):
        env = dict(fix)
        env.update({v: bool((i >> k) & 1) for k, v in enumerate(free)})
        if nxt.eval({**env, iq: False}) != nxt.eval({**env, iq: True}):
            return False
    return True


def cell_fn(cell: Group, out: str) -> str:
    return _pins(cell)[out].attrs.get("function", "?")


def generate_cell_map(
    library: Group, core: Any, *, internal_cells: Mapping[str, Any] | None = None
) -> GenerationResult:
    """The cell map of every cell in ``library`` (a parsed Liberty ``library`` group),
    with ``internal_cells`` (FaultFlow's own cells: the scan flop it stitches, observe
    buffers) first. ``core`` is the C++ extension, for its gate evaluation."""
    gen = _Generator(core)
    cells = [c for c in library.all("cell") if c.args]
    names = [str(c.args[0]) for c in cells]
    entries = {str(c.args[0]): gen.entry(c) for c in cells}

    by_base: dict[str, list[str]] = {}
    for name in names:
        by_base.setdefault(_base(name), []).append(name)
    out: dict[str, Any] = dict(internal_cells or {})
    keyed: dict[str, Any] = {}
    for base, members in by_base.items():
        pattern = base + "_*"
        same = all(entries[m] == entries[members[0]] for m in members)
        clash = any(
            fnmatch.fnmatchcase(other, pattern)
            for other in names
            if other not in members
        )
        sized = all(re.search(r"_\d+$", m) for m in members)
        if same and sized and not clash:
            keyed[pattern] = entries[members[0]]
        else:
            for m in members:
                keyed[m] = entries[m]
    for key in sorted(keyed):
        out[key] = keyed[key]
    return GenerationResult(out, gen.report)
