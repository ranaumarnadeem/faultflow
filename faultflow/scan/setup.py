"""Test setup programs (``[scan] test_setup``): the cycles a tester applies before a
scan pattern to put the chip in its test mode -- a test-mode register loaded through a
port of its own, say.

A program is a list of cycles. In each, every input of the chip but its scan clocks is
at the value the cycle gives it, 0 if it gives none; the scan clocks the cycle pulses go
from their off level to on and back at its end, with the cycle's inputs, and the others
stay off. A clock that isn't a scan clock (a TCK) is an input like any other, at a
level. The program is its own: nothing else -- ``[scan] hold`` included -- sets an
input during it.

FaultFlow grades every pattern after its setup program, fault-free and with nothing
compared (``scan_pattern_sim.cpp``); the non-scan flops it leaves at a value they then
hold are *programmed* (:mod:`faultflow.scan.nonscan`). A tester applies the program
before each pattern, or once, first, when the patterns overlap
(:mod:`faultflow.scan.tester_program`); the STIL writes it as the ``test_setup`` macro.

The file is JSON::

    {"format": "faultflow_test_setup_v1",
     "cycles": [{"inputs": {"cfg_rst_n": 0}},
                {"inputs": {"cfg_rst_n": 1, "cfg_en": 1, "cfg_in": 1},
                 "pulse": ["cfg_clk"]}]}

An input bit is named as a pattern names it: the port, or ``port[i]`` for bit i of a
bus.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.scan.errors import ScanError

SETUP_FORMAT = "faultflow_test_setup_v1"


@dataclass(frozen=True)
class SetupCycle:
    """One cycle of a setup program: each input it names at its value (any other
    input at 0), then the scan clocks in ``pulse`` pulsed."""

    inputs: tuple[tuple[str, int], ...] = ()
    pulse: tuple[str, ...] = ()

    def value(self, name: str) -> int:
        return dict(self.inputs).get(name, 0)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"inputs": dict(self.inputs)}
        if self.pulse:
            out["pulse"] = list(self.pulse)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "setup") -> SetupCycle:
        if not isinstance(data, Mapping):
            raise ScanError(f"{where}: a cycle is an object with inputs and pulse")
        unknown = sorted(set(data) - {"inputs", "pulse"})
        if unknown:
            raise ScanError(f"{where}: unknown keys {unknown} (inputs, pulse)")
        inputs = data.get("inputs", {})
        if not isinstance(inputs, Mapping):
            raise ScanError(f"{where}: inputs is an object of input bit -> 0/1")
        values: dict[str, int] = {}
        for name, value in inputs.items():
            if value not in (0, 1) or isinstance(value, float):
                raise ScanError(f"{where}: input {name} is {value!r}, not 0 or 1")
            values[str(name)] = int(value)
        pulse = data.get("pulse", [])
        if not isinstance(pulse, list) or not all(isinstance(p, str) for p in pulse):
            raise ScanError(f"{where}: pulse is a list of scan clock names")
        if len(set(pulse)) != len(pulse):
            raise ScanError(f"{where}: pulse names a clock twice")
        return cls(tuple(sorted(values.items())), tuple(pulse))


def setup_to_list(cycles: Sequence[SetupCycle]) -> list[dict[str, Any]]:
    return [cycle.to_dict() for cycle in cycles]


def setup_from_list(data: Any, where: str = "setup") -> tuple[SetupCycle, ...]:
    if not isinstance(data, list):
        raise ScanError(f"{where}: cycles is a list")
    return tuple(
        SetupCycle.from_dict(cycle, f"{where} cycle {index}")
        for index, cycle in enumerate(data)
    )


def load_setup(path: Path) -> tuple[SetupCycle, ...]:
    """The program in ``path`` (a test setup file, see the module docstring)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(f"[scan] test_setup {path}: {exc}") from exc
    where = f"[scan] test_setup {path}"
    if not isinstance(data, dict) or data.get("format") != SETUP_FORMAT:
        raise ScanError(f'{where}: not a test setup file ("format": "{SETUP_FORMAT}")')
    unknown = sorted(set(data) - {"format", "cycles"})
    if unknown:
        raise ScanError(f"{where}: unknown keys {unknown} (format, cycles)")
    cycles = setup_from_list(data.get("cycles"), where)
    if not cycles:
        raise ScanError(f"{where}: no cycles")
    return cycles


def check_setup(
    cycles: Sequence[SetupCycle], inputs: Sequence[str], clocks: Sequence[str]
) -> None:
    """Refuse a program that names anything but the chip's input bits ``inputs``,
    sets a scan clock (``clocks``) as a level, or pulses anything else."""
    known = set(inputs)
    scan_clocks = set(clocks)
    for index, cycle in enumerate(cycles):
        for name, _ in cycle.inputs:
            if name in scan_clocks:
                raise ScanError(
                    f"test setup cycle {index}: {name} is a scan clock; pulse it "
                    "(pulse) instead of setting it"
                )
            if name not in known:
                raise ScanError(
                    f"test setup cycle {index}: {name} is not an input of the chip"
                )
        for name in cycle.pulse:
            if name not in scan_clocks:
                raise ScanError(
                    f"test setup cycle {index}: pulses {name}, which is not a scan "
                    f"clock ({', '.join(sorted(scan_clocks)) or 'none'})"
                )


def setup_request(
    cycles: Sequence[SetupCycle],
) -> list[tuple[dict[str, bool], list[str]]]:
    """The program as the core's scan simulation takes it (setup_cycles): per
    cycle, its inputs and the clocks it pulses."""
    return [
        ({name: bool(value) for name, value in cycle.inputs}, list(cycle.pulse))
        for cycle in cycles
    ]


def setup_hash(cycles: Sequence[SetupCycle]) -> str:
    """What the campaign fingerprint records of the program: "" for none."""
    if not cycles:
        return ""
    text = json.dumps(setup_to_list(cycles), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def setup_test_cycles(
    cycles: Sequence[SetupCycle], clock_off: Mapping[str, int]
) -> list[tuple[dict[str, int], dict[str, int]]]:
    """The program as the core runs it, one step per (inputs, clocks) pair: each
    cycle's inputs with the scan clocks (``clock_off``: each one's off level) off,
    then, when it pulses some, them on, then off again -- a falling-edge flop takes
    its value with the cycle's inputs, as on a tester. scan_pattern_sim.cpp builds
    the same steps."""
    steps: list[tuple[dict[str, int], dict[str, int]]] = []
    for cycle in cycles:
        inputs = dict(cycle.inputs)
        off = dict(clock_off)
        steps.append((inputs, off))
        if cycle.pulse:
            on = dict(off)
            for clock in cycle.pulse:
                on[clock] = 1 - off[clock]
            steps.append((inputs, on))
            steps.append((inputs, dict(off)))
    return steps
