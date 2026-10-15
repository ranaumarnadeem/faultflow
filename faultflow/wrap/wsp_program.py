"""The integrity program of an IEEE 1500 WSP (faultflow.wrap.wsp): tester cycles
played on the WSP alone, each with what its behavioural model
(faultflow.wrap.wsp_model) expects at WSO, at the wrapped outputs and at the
wrapper chains' scan-outs -- what ``ff.py wsp`` grades the WSP's own faults with,
the faults every scan test leaves to it (``excluded_wsp``).

The program, test by test (:func:`integrity_program`):

- ``reset``: WRSTN low: WS_BYPASS, WBY cleared.
- ``wir_shift``: a pattern through the WIR's shift stage, SelectWIR and ShiftWR
  high: WSO is the bit that went in three shifts before.
- per instruction code, all eight (``code_<bits>``): the code loaded (WRSTN, the
  shift, the update), then the data register it selects shifted with a pattern
  -- WBY, one bit, for WS_BYPASS, the parallel instructions and the unused codes;
  the boundary register from WSI to WSO through every wrapper chain for WS_EXTEST
  and WS_INTEST -- then a capture and a shift-out: WBY captures 0, and in
  WS_EXTEST the input cells capture the ports, which the program drives with a
  pattern.
- ``held_update``: a code shifted in, then UpdateWR with SelectWIR low: the
  instruction in force stays.

Every input but the WSP's and the wrapped data inputs is 0 throughout: the scan
enable, the core's clocks, its resets. What the model can't know -- the core's
outputs an output cell captures in WS_INTEST, any register before its first load
-- is no care.

A program is a :class:`WspProgram`: the steps :func:`legacy_integrity_program` builds
from the wrapper record, or those a ``warptap-wsp-program`` file (warptap's
``build_wsp_integrity_program``, written by its ``to_json``) reads to against the record
(:meth:`WspProgram.from_json`). The file's columns are the WSP's signals; its ``drive``
names are the wrapped input cells' ports, and its ``observe`` points WSO, the wrapped
output ports (by label) and the wrapper chains' scan-outs (by chain index: the block's
port for that chain is the record's, not the name the file gives it). A file that isn't
that format, or doesn't fit the wrapper, is refused before anything is graded.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from faultflow.wrap.cell import INPUT
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import (
    OPCODES,
    SERIAL,
    WIR_BITS,
    WspChainModel,
    WspCycle,
    WspModel,
    instruction,
    program,
)

FORMAT = "warptap-wsp-program"
VERSION = 1
ROLES = ("wsi", "wso", "shiftwr", "capturewr", "updatewr", "selectwir", "wrck", "wrstn")
_COLUMNS = ("wrstn", "selectwir", "shiftwr", "capturewr", "updatewr", "wsi", "pulse")


class WspProgramError(WrapError):
    """A WSP program file that isn't one, or doesn't fit the wrapper; the message says
    why."""


@dataclass(frozen=True)
class WspStep:
    """A tester cycle of the program: the WSP's inputs (``cycle``), the wrapped
    data inputs' values by label (input cells' ports; any other at 0), the test
    it belongs to, the WSO the model expects at its strobe (None: no care), and
    what it expects of the other outputs there: each wrapped output port, by
    label -- 0 where the instruction makes it safe, its cell's flop where it
    holds -- and each wrapper chain's scan-out, its last cell's flop."""

    cycle: WspCycle
    ports: tuple[tuple[str, int], ...]
    test: str
    wso: int | None
    expected: tuple[tuple[str, int | None], ...] = ()


def serial_cells(wrapper: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A block's boundary cells in the boundary register's serial order: wrapper
    chain by chain (faultflow.wrap.wsp.link_wsp strings them in index order), each
    from its head."""
    return sorted(wrapper["cells"], key=lambda c: (int(c["chain"]), int(c["position"])))


def _pattern(length: int, seed: int) -> list[int]:
    """`length` bits of a 4-bit LFSR's sequence from `seed`: both values and both
    transitions within a few bits."""
    state = 0b1011 ^ (seed & 0b1111) or 0b1011
    bits: list[int] = []
    for _ in range(length):
        bits.append(state & 1)
        feedback = ((state >> 3) ^ (state >> 2)) & 1
        state = ((state << 1) | feedback) & 0b1111
    return bits


def _check_block(wrapper: Mapping[str, Any]) -> None:
    if wrapper.get("control") != "wsp" or "blocks" in wrapper:
        raise WrapError("ff.py wsp grades a block's IEEE 1500 WSP")


def is_soc(wrapper: Mapping[str, Any]) -> bool:
    """Whether ``wrapper`` records a SoC's chain of WSPs (faultflow.project.soc_wrapper)
    rather than a block's."""
    return "blocks" in wrapper


def check_wsp(wrapper: Mapping[str, Any]) -> None:
    """Refuse a wrapper record whose mode isn't set by a WSP: a block's or a SoC's is
    what ``ff.py wsp`` grades."""
    if wrapper.get("control") != "wsp":
        raise WrapError("ff.py wsp grades a block's or a SoC's IEEE 1500 WSP")


def roles_of(wrapper: Mapping[str, Any]) -> dict[str, str]:
    """The port of each WSP role (:data:`ROLES`) on the netlist: a block's own, or
    a SoC's pin that drives it (through an inverter, if its record says so)."""
    check_wsp(wrapper)
    wsp = wrapper["wsp"]
    if is_soc(wrapper):
        found = {role: str(pin["port"]) for role, pin in wsp["pins"].items()}
        found["wrck"] = str(wsp["wrck"])
        found["wso"] = str(wsp["wso"])
        return {role: found[role] for role in ROLES}
    return {role: str(wsp["ports"][role]["port"]) for role in ROLES}


def soc_blocks(
    wrapper: Mapping[str, Any],
) -> list[tuple[str, str, list[list[tuple[str, str]]]]]:
    """A SoC's blocks from WSI to WSO: for each its instance, its block's name and its
    boundary register's wrapper chains, each a list of (label, side) from its head
    -- the order of the block's own WSP, which the SoC's scan chains replace in the
    cell records' ``chain`` and ``position``."""
    check_wsp(wrapper)
    if not is_soc(wrapper):
        raise WrapError("a block's wrapper has no chain of blocks")
    named = {str(b["instance"]): str(b["block"]) for b in wrapper["blocks"]}
    order = [str(instance) for instance in wrapper["wsp"].get("chain", [])]
    if not order:
        raise WrapError("the SoC's wrapper record has no WSP chain")
    found: list[tuple[str, str, list[list[tuple[str, str]]]]] = []
    for instance in order:
        if instance not in named:
            raise WrapError(f"the SoC's WSP chain names {instance}, which is no block")
        cells = [c for c in wrapper["cells"] if str(c.get("instance")) == instance]
        if any("block_chain" not in c for c in cells):
            raise WrapError(
                "the SoC's wrapper record predates block_chain: scan it again"
            )
        cells.sort(key=lambda c: (int(c["block_chain"]), int(c["block_position"])))
        chains: dict[int, list[tuple[str, str]]] = {}
        for c in cells:
            chains.setdefault(int(c["block_chain"]), []).append(
                (str(c["label"]), str(c["side"]))
            )
        found.append((instance, named[instance], [chains[k] for k in sorted(chains)]))
    return found


def legacy_integrity_program(
    wrapper: Mapping[str, Any], scan_outs: Mapping[int, str] | None = None
) -> list[WspStep]:
    """The integrity program (see the module docstring) of the WSP a block's
    wrapper record (faultflow.wrap.record, control "wsp") names, as faultflow's own
    builder writes it. `scan_outs`: each wrapper chain's scan-out port, by chain
    index, to compare too."""
    _check_block(wrapper)
    cells = serial_cells(wrapper)
    sides = [str(cell["side"]) for cell in cells]
    labels = [str(cell["label"]) for cell in cells]
    inputs = [label for side, label in zip(sides, labels) if side == INPUT]
    tails = {
        port: max(
            (k for k, cell in enumerate(cells) if int(cell["chain"]) == chain),
            default=None,
        )
        for chain, port in (scan_outs or {}).items()
    }
    model = WspModel(sides)
    steps: list[WspStep] = []

    def run(test: str, cycle: WspCycle, ports: Mapping[str, int] | None = None) -> None:
        given = {label: int((ports or {}).get(label, 0)) for label in inputs}
        cfi = tuple(
            given[label] if side == INPUT else None
            for side, label in zip(sides, labels)
        )
        full = dataclasses.replace(cycle, cfi=cfi)
        out = model.outputs(full)
        expected = [
            (label, out[k])
            for k, (side, label) in enumerate(zip(sides, labels))
            if side != INPUT
        ]
        expected += [
            (port, None if tail is None else model.wbr[tail])
            for port, tail in tails.items()
        ]
        wso = model.step(full)
        steps.append(
            WspStep(cycle, tuple(sorted(given.items())), test, wso, tuple(expected))
        )

    def load(test: str, code: int) -> None:
        for cycle in program(code):
            run(test, cycle)

    run("reset", WspCycle(wrstn=0, pulse=False))
    for bit in _pattern(WIR_BITS + 8, 1):
        run("wir_shift", WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    for code in range(1 << WIR_BITS):
        test = f"code_{code:0{WIR_BITS}b}"
        load(test, code)
        serial = instruction(code) in SERIAL
        length = len(cells) if serial else 1
        for bit in _pattern(length + 2, code + 2):
            run(test, WspCycle(shiftwr=1, wsi=bit))
        captured = {
            label: bit for label, bit in zip(inputs, _pattern(len(inputs), code))
        }
        run(test, WspCycle(capturewr=1), captured)
        for bit in _pattern(length + 1, code + 3):
            run(test, WspCycle(shiftwr=1, wsi=bit))
    # UpdateWR with SelectWIR low updates nothing: WS_EXTEST stays in force.
    load("held_update", OPCODES["WS_EXTEST"])
    for bit in reversed([0, 1, 0]):  # WP_INTEST, shifted in
        run("held_update", WspCycle(selectwir=1, shiftwr=1, wsi=bit))
    run("held_update", WspCycle(updatewr=1))
    for bit in _pattern(len(cells) + 1, 9):
        run("held_update", WspCycle(shiftwr=1, wsi=bit))
    if model.instruction != "WS_EXTEST":
        raise WrapError("the integrity program's held update changed the instruction")
    return steps


def program_tests(steps: Sequence[WspStep]) -> list[str]:
    """The program's tests, in order."""
    seen: list[str] = []
    for step in steps:
        if not seen or seen[-1] != step.test:
            seen.append(step.test)
    return seen


integrity_program = legacy_integrity_program


def _bits(column: Sequence[int]) -> str:
    return "".join(str(int(bit)) for bit in column)


@dataclass(frozen=True)
class WspProgram:
    """The steps ``ff.py wsp`` grades with, and where they came from: ``source`` is
    ``"builder"`` (:func:`legacy_integrity_program`) or ``"file"``."""

    steps: tuple[WspStep, ...]
    source: str = "builder"

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def tests(self) -> list[str]:
        return program_tests(self.steps)

    @classmethod
    def build(
        cls, wrapper: Mapping[str, Any], scan_outs: Mapping[int, str] | None = None
    ) -> "WspProgram":
        return cls(tuple(legacy_integrity_program(wrapper, scan_outs)), "builder")

    def to_json(
        self, wrapper: Mapping[str, Any], scan_outs: Mapping[int, str] | None = None
    ) -> dict[str, Any]:
        """The program as a ``warptap-wsp-program`` (version 1) for ``wrapper``."""
        check_wsp(wrapper)
        soc = is_soc(wrapper)
        roles = roles_of(wrapper)
        if not self.steps:
            raise WspProgramError("a WSP program has no cycles")
        if soc:
            soc_blocks(wrapper)  # a record that can't be the program's is refused
            if self.steps[0].expected or any(step.ports for step in self.steps):
                raise WspProgramError(
                    "a SoC's program drives no port and observes WSO alone"
                )
        cells = [] if soc else serial_cells(wrapper)
        inputs = [str(c["label"]) for c in cells if c["side"] == INPUT]
        outputs = {
            str(c["label"]): k for k, c in enumerate(cells) if c["side"] != INPUT
        }
        chains = sorted({int(c["chain"]) for c in cells})
        by_port = {port: chain for chain, port in (scan_outs or {}).items()}
        observe: list[dict[str, Any]] = [{"name": roles["wso"], "kind": "wso"}]
        for name, _ in self.steps[0].expected:
            if name in outputs:
                observe.append({"name": name, "kind": "output", "index": outputs[name]})
            elif name in by_port and by_port[name] in chains:
                index = chains.index(by_port[name])
                observe.append({"name": name, "kind": "scan_out", "index": index})
            else:
                raise WspProgramError(
                    f"{name} is neither an output cell nor a scan-out"
                )
        for point in observe:
            point["wrapper"] = 0
        expect: dict[str, dict[str, str]] = {}
        for k, point in enumerate(observe):
            values = [
                step.wso if k == 0 else dict(step.expected).get(str(point["name"]))
                for step in self.steps
            ]
            expect[str(point["name"])] = {
                "value": "".join("0" if v is None else str(v) for v in values),
                "care": "".join("0" if v is None else "1" for v in values),
            }
        ranges: list[dict[str, Any]] = []
        for number, step in enumerate(self.steps):
            if ranges and ranges[-1]["name"] == step.test:
                ranges[-1]["stop"] = number + 1
            else:
                ranges.append({"name": step.test, "start": number, "stop": number + 1})
        data: dict[str, Any] = {
            "format": FORMAT,
            "version": VERSION,
            "roles": roles,
            "drive": inputs,
            "observe": observe,
            "tests": ranges,
        }
        for column in _COLUMNS:
            data[column] = _bits([int(getattr(s.cycle, column)) for s in self.steps])
        ported = [dict(step.ports) for step in self.steps]
        data["inputs"] = {label: _bits([p[label] for p in ported]) for label in inputs}
        data["expect"] = expect
        return data

    @classmethod
    def from_text(
        cls,
        text: str,
        wrapper: Mapping[str, Any],
        scan_outs: Mapping[int, str] | None = None,
    ) -> "WspProgram":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise WspProgramError(f"the WSP program isn't JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise WspProgramError("the WSP program isn't a JSON object")
        return cls.from_json(data, wrapper, scan_outs)

    @classmethod
    def from_json(
        cls,
        data: Mapping[str, Any],
        wrapper: Mapping[str, Any],
        scan_outs: Mapping[int, str] | None = None,
    ) -> "WspProgram":
        """The steps of the ``warptap-wsp-program`` ``data`` for the block or SoC
        ``wrapper`` describes (see the module docstring)."""
        check_wsp(wrapper)
        soc = is_soc(wrapper)
        if soc:
            soc_blocks(wrapper)
        if data.get("format") != FORMAT:
            raise WspProgramError(
                f"not a {FORMAT} file (format={data.get('format')!r})"
            )
        if data.get("version") != VERSION:
            raise WspProgramError(
                f"{FORMAT} version {data.get('version')!r}: "
                f"this reads version {VERSION}"
            )
        cells = [] if soc else serial_cells(wrapper)
        sides = [str(c["side"]) for c in cells]
        labels = [str(c["label"]) for c in cells]
        record_inputs = {label for side, label in zip(sides, labels) if side == INPUT}
        record_outputs = {label for side, label in zip(sides, labels) if side != INPUT}
        chains = sorted({int(c["chain"]) for c in cells})
        try:
            roles = {str(k): str(v) for k, v in data["roles"].items()}
            drive = [str(name) for name in data["drive"]]
            columns = {name: str(data[name]) for name in _COLUMNS}
            count = len(columns["wrstn"])
            inputs = {name: str(data["inputs"][name]) for name in drive}
            points = [
                (
                    str(o["name"]),
                    str(o["kind"]),
                    int(o["wrapper"]),
                    int(o.get("index", 0)),
                )
                for o in data["observe"]
            ]
            expect = {
                name: (
                    str(data["expect"][name]["value"]),
                    str(data["expect"][name]["care"]),
                )
                for name, _, _, _ in points
            }
            tests = [
                (str(t["name"]), int(t["start"]), int(t["stop"])) for t in data["tests"]
            ]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise WspProgramError(f"malformed WSP program: {exc}") from exc
        if count == 0:
            raise WspProgramError("the WSP program has no cycles")
        every = [*columns.values(), *inputs.values()]
        for value, care in expect.values():
            every += [value, care]
        if any(len(col) != count or set(col) - {"0", "1"} for col in every):
            raise WspProgramError(
                "every column of a WSP program is 0/1 and as long as the cycle count"
            )
        mine = roles_of(wrapper)
        for role in ROLES:
            if roles.get(role) != mine[role]:
                raise WspProgramError(
                    f"role {role} is {roles.get(role)!r} in the program, "
                    f"{mine[role]!r} on the {'SoC' if soc else 'block'}"
                )
        extra = sorted(set(drive) - record_inputs)
        if extra:
            raise WspProgramError(
                f"the program drives {extra[0]}, which is no input cell of the "
                f"{'SoC' if soc else 'block'}"
            )
        lacking = sorted(record_inputs - set(drive))
        if lacking:
            raise WspProgramError(f"the program doesn't drive input cell {lacking[0]}")
        wso = [p for p in points if p[1] == "wso"]
        if len(wso) != 1:
            raise WspProgramError("the program must observe WSO exactly once")
        observed: list[tuple[str, str]] = []  # (name in a step's expected, point name)
        for name, kind, which, index in points:
            if kind == "wso":
                continue
            if soc:
                raise WspProgramError(
                    f"the program observes {name}, which a SoC's WSP chain has no "
                    "pin for: WSO alone"
                )
            if which != 0:
                raise WspProgramError(
                    f"{name} is in wrapper {which}: a block has one wrapper"
                )
            if kind == "output":
                if name in record_inputs:
                    raise WspProgramError(
                        f"{name} is an input cell, not an output cell of the block"
                    )
                if name not in record_outputs:
                    raise WspProgramError(f"{name} is no output cell of the block")
                observed.append((name, name))
            elif kind == "scan_out":
                if not 0 <= index < len(chains):
                    raise WspProgramError(
                        f"{name}: the block has no wrapper chain {index} "
                        "for its scan-out"
                    )
                port = (scan_outs or {}).get(chains[index])
                if port is None:
                    raise WspProgramError(
                        f"{name}: no scan-out port for wrapper chain {chains[index]}"
                    )
                observed.append((port, name))
            else:
                raise WspProgramError(f"{name}: an observation point of kind {kind!r}")
        owner = [""] * count
        for name, start, stop in tests:
            if not 0 <= start < stop <= count:
                raise WspProgramError(
                    f"test {name}: range [{start}, {stop}) is out of the program"
                )
            if any(owner[start:stop]):
                raise WspProgramError(f"test {name} overlaps another test")
            owner[start:stop] = [name] * (stop - start)
        if not all(owner):
            raise WspProgramError("the tests must cover every cycle")
        wso_value, wso_care = expect[wso[0][0]]
        steps: list[WspStep] = []
        for n in range(count):
            given = {label: int(inputs[label][n]) for label in drive}
            cycle = WspCycle(
                wrstn=int(columns["wrstn"][n]),
                selectwir=int(columns["selectwir"][n]),
                shiftwr=int(columns["shiftwr"][n]),
                capturewr=int(columns["capturewr"][n]),
                updatewr=int(columns["updatewr"][n]),
                wsi=int(columns["wsi"][n]),
                pulse=columns["pulse"][n] == "1",
            )
            seen = []
            for port, name in observed:
                value, care = expect[name]
                seen.append((port, int(value[n]) if care[n] == "1" else None))
            steps.append(
                WspStep(
                    cycle,
                    tuple(sorted(given.items())),
                    owner[n],
                    int(wso_value[n]) if wso_care[n] == "1" else None,
                    tuple(seen),
                )
            )
        return cls(tuple(steps), "file")


def conformance_failures(
    program: WspProgram,
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str] | None = None,
) -> list[str]:
    """Where ``program`` expects something faultflow's own model of the WSP
    (faultflow.wrap.wsp_model) doesn't: the program's cycles and the ports it drives
    replayed on the model, WSO, each wrapped output and each scan-out compared at every
    care bit. A care bit where the model can't know the value (a register not yet
    loaded, a cell that captured the core) is a failure too -- the program claims what
    nothing defines. A no-care bit never fails. Empty: the program and the model agree,
    so the netlist the golden gate holds to the program is held to the model as well.
    A SoC's chain is replayed on a chain of models, the first block's WSI the chip's."""
    check_wsp(wrapper)
    if is_soc(wrapper):
        return _soc_conformance_failures(program, wrapper)
    _check_block(wrapper)
    cells = serial_cells(wrapper)
    sides = [str(c["side"]) for c in cells]
    labels = [str(c["label"]) for c in cells]
    tails = {
        port: max(
            (k for k, c in enumerate(cells) if int(c["chain"]) == chain), default=None
        )
        for chain, port in (scan_outs or {}).items()
    }
    model = WspModel(sides)
    failures: list[str] = []

    def disagree(number: int, step: WspStep, name: str, want: Any, got: Any) -> None:
        failures.append(
            f"cycle {number} ({step.test}): {name} is {want} in the program, "
            f"{'unknown' if got is None else got} in the model"
        )

    for number, step in enumerate(program.steps):
        given = dict(step.ports)
        cfi = tuple(
            given.get(label, 0) if side == INPUT else None
            for side, label in zip(sides, labels)
        )
        full = dataclasses.replace(step.cycle, cfi=cfi)
        out = model.outputs(full)
        modelled: dict[str, Any] = {
            label: out[k]
            for k, (side, label) in enumerate(zip(sides, labels))
            if side != INPUT
        }
        modelled.update(
            {
                port: None if tail is None else model.wbr[tail]
                for port, tail in tails.items()
            }
        )
        strobed = model.step(full)
        if step.wso is not None and step.wso != strobed:
            disagree(number, step, "wso", step.wso, strobed)
        for name, value in step.expected:
            if value is None:
                continue
            if name not in modelled:
                failures.append(
                    f"cycle {number} ({step.test}): the program observes {name}, "
                    "which is no output or scan-out of the block"
                )
            elif modelled[name] != value:
                disagree(number, step, name, value, modelled[name])
    return failures


def _soc_conformance_failures(
    program: WspProgram, wrapper: Mapping[str, Any]
) -> list[str]:
    """:func:`conformance_failures` of a SoC's chain: WSO alone, every cell's
    functional input unknown (a block port is the SoC's own net)."""
    model = WspChainModel(
        [
            [side for chain in chains for _, side in chain]
            for _, _, chains in soc_blocks(wrapper)
        ]
    )
    failures: list[str] = []
    for number, step in enumerate(program.steps):
        strobed = model.step(step.cycle)
        if step.expected:
            failures.append(
                f"cycle {number} ({step.test}): the program observes "
                f"{step.expected[0][0]}, which a SoC's chain has no pin for"
            )
        if step.wso is not None and step.wso != strobed:
            failures.append(
                f"cycle {number} ({step.test}): wso is {step.wso} in the program, "
                f"{'unknown' if strobed is None else strobed} in the model"
            )
    return failures
