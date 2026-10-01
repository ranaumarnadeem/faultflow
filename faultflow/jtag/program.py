"""TCK programs for ``ff.py jtag``: the TMS/TDI/TRST_N to drive each TCK period and the
TDO each shift should show, in warptap's ``warptap-tck-program`` v1 format.

A program comes from warptap's ``build_integrity_program`` (the network rebuilt from an
autoMBIST manifest, or any warptap network) or from a file in the same format, so a
design warptap didn't build, or a machine without warptap, can still be graded.
faultflow only reads the format; it never needs warptap to grade.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

PROGRAM_FORMAT = "warptap-tck-program"
PROGRAM_VERSION = 1
_COLUMNS = ("tms", "tdi", "trst_n", "shift", "tdo", "care")


class JtagProgramError(ValueError):
    """A TCK program that can't be read, or can't be built from the given network."""


@dataclass(frozen=True)
class TapParams:
    """The TAP a program targets. ``ijtag_access_opcode``: the instruction that selects
    the network, for a TAP built with one (warptap's IJTAG_ACCESS); None, EXTEST."""

    ir_width: int
    has_idcode: bool
    idcode_value: int
    ijtag_access_opcode: int | None = None


@dataclass(frozen=True)
class TckProgram:
    """One entry per TCK period in each column (a string of ``0``/``1``): ``tms``,
    ``tdi``, ``trst_n`` are driven for the period; on a ``shift`` period TDO is sampled
    before the rising edge and should equal ``tdo`` where ``care``. ``tests`` names the
    period ranges ``[start, stop)`` in program order."""

    tap: TapParams
    tests: tuple[tuple[str, int, int], ...]
    tms: str
    tdi: str
    trst_n: str
    shift: str
    tdo: str
    care: str

    def __len__(self) -> int:
        return len(self.tms)

    def test_of(self, period: int) -> str:
        for name, start, stop in self.tests:
            if start <= period < stop:
                return name
        raise IndexError(period)

    def shift_periods(self) -> list[int]:
        """The periods with a TDO sample, in order: sample ``s`` is period
        ``shift_periods()[s]``."""
        return [i for i, bit in enumerate(self.shift) if bit == "1"]

    def to_json(self) -> dict[str, Any]:
        tap: dict[str, Any] = {
            "ir_width": self.tap.ir_width,
            "has_idcode": self.tap.has_idcode,
            "idcode_value": self.tap.idcode_value,
        }
        if self.tap.ijtag_access_opcode is not None:
            tap["ijtag_access_opcode"] = self.tap.ijtag_access_opcode
        return {
            "format": PROGRAM_FORMAT,
            "version": PROGRAM_VERSION,
            "tap": tap,
            "tests": [
                {"name": name, "start": start, "stop": stop}
                for name, start, stop in self.tests
            ],
            **{column: getattr(self, column) for column in _COLUMNS},
        }

    def digest(self) -> str:
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TckProgram:
        if (
            data.get("format") != PROGRAM_FORMAT
            or data.get("version") != PROGRAM_VERSION
        ):
            raise JtagProgramError(
                f"not a {PROGRAM_FORMAT} v{PROGRAM_VERSION} program "
                f"(format={data.get('format')!r}, version={data.get('version')!r})"
            )
        try:
            columns = {c: str(data[c]) for c in _COLUMNS}
            tap = data["tap"]
            access = tap.get("ijtag_access_opcode")
            params = TapParams(
                ir_width=int(tap["ir_width"]),
                has_idcode=bool(tap["has_idcode"]),
                idcode_value=int(tap["idcode_value"]),
                ijtag_access_opcode=None if access is None else int(access),
            )
            tests = tuple(
                (str(t["name"]), int(t["start"]), int(t["stop"])) for t in data["tests"]
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise JtagProgramError(f"malformed TCK program: {exc}") from exc
        count = len(columns["tms"])
        if count == 0:
            raise JtagProgramError("TCK program has no cycles")
        for name, column in columns.items():
            if len(column) != count or set(column) - {"0", "1"}:
                raise JtagProgramError(
                    f"TCK program column {name!r} must be {count} characters of 0/1"
                )
        expected_start = 0
        for name, start, stop in tests:
            if start != expected_start or stop <= start:
                raise JtagProgramError(
                    f"TCK program test {name!r} has range {start}..{stop}"
                )
            expected_start = stop
        if expected_start != count:
            raise JtagProgramError("TCK program tests don't cover every cycle")
        if any(
            c == "1" and s == "0" for c, s in zip(columns["care"], columns["shift"])
        ):
            raise JtagProgramError("TCK program marks a non-shift cycle as care")
        return cls(tap=params, tests=tests, **columns)


def load_program(path: str | Path) -> TckProgram:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JtagProgramError(f"cannot read TCK program {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise JtagProgramError(f"TCK program {path} is not a JSON object")
    return TckProgram.from_json(data)


def program_from_warptap(
    graph: Any,
    root: Any,
    *,
    ir_width: int,
    has_idcode: bool,
    idcode_value: int,
    margin: int,
    exhaustive_opcodes: bool,
    ijtag_access_opcode: int | None = None,
) -> TckProgram:
    """warptap's integrity program for a network it describes (needs warptap
    importable, with ``warptap.tap_integrity``), reached through IJTAG_ACCESS at
    ``ijtag_access_opcode``, or EXTEST if None."""
    try:
        from warptap.tap_integrity import (  # type: ignore[import-not-found]
            TapConfig,
            TapIntegrityError,
            build_integrity_program,
        )
    except ImportError as exc:
        raise JtagProgramError(
            "building the JTAG integrity program needs warptap with tap_integrity "
            "importable; pass a program file instead"
        ) from exc
    params: dict[str, Any] = {
        "ir_width": ir_width,
        "has_idcode": has_idcode,
        "idcode_value": idcode_value,
    }
    if ijtag_access_opcode is not None:
        if "ijtag_access_opcode" not in {f.name for f in fields(TapConfig)}:
            raise JtagProgramError(
                "the installed warptap can't build a program for a network behind "
                "IJTAG_ACCESS (it predates ijtag_access_opcode); update it, or pass a "
                "program file"
            )
        params["ijtag_access_opcode"] = ijtag_access_opcode
    try:
        program = build_integrity_program(
            graph,
            root,
            tap=TapConfig(**params),
            margin=margin,
            exhaustive_opcodes=exhaustive_opcodes,
        )
    except TapIntegrityError as exc:
        raise JtagProgramError(f"warptap can't build the program: {exc}") from exc
    return TckProgram.from_json(program.to_json())
