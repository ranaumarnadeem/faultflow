"""The cells FaultFlow instantiates itself, by role, from any library's cell map.

A role is a logical function with logical pins. A combinational role -- ``and2``,
``or2``, ``xor2``, ``inv``, ``and2b`` (``!a_n & b``), ``mux2`` (non-inverting:
``s ? a1 : a0``) -- is filled by a GATE entry whose gate type, under some mapping of the
logical pins onto its ordered inputs, is that function: checked in every input
combination with the core's own gate evaluation. A flop role is filled by an FF entry
with exactly that shape: the clock edge, an active-low clear or preset, a scan mux
(scan enable active high), nothing else; one without an inverted output is preferred.

A role's cell is its entry's drive strength 1, else its smallest: of the
entry's ``variants`` (the concrete names a generated map records), else the pattern
with ``*`` read as ``1``."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class PdkError(RuntimeError):
    """A library that can't give FaultFlow a cell it needs."""


# The cell-map field naming the extra library an entry is from
# (faultflow.pdk.resolve.combine_libraries); the primary library's entries have none.
LIBRARY_KEY = "library"


def library_maps(cell_map: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A combined cell map split by library: ``""`` -> the primary library's
    entries and FaultFlow's own; each extra library's name -> its entries, as its
    own map has them (untagged)."""
    split: dict[str, dict[str, Any]] = {"": {}}
    for key, entry in cell_map.items():
        name = entry.get(LIBRARY_KEY, "") if isinstance(entry, Mapping) else ""
        if name:
            entry = {k: v for k, v in entry.items() if k != LIBRARY_KEY}
        split.setdefault(str(name), {})[key] = entry
    return split


@dataclass(frozen=True)
class RoleCell:
    cell: str
    pins: Mapping[str, str]  # logical pin -> the cell's pin ("out" / "q": the output)
    # The cell's pins held at a constant ("0" / "1"): a flop role filled by a flop
    # with more controls than it needs (IHP SG13G2 has reset flops only) holds the
    # rest inactive.
    ties: Mapping[str, str] = field(default_factory=dict)


_COMB: dict[str, tuple[tuple[str, ...], Callable[..., bool], str, str]] = {
    # role: (logical inputs, function, preferred gate type, preferred cell name)
    "and2": (("a", "b"), lambda a, b: a and b, "AND2", "and2"),
    "or2": (("a", "b"), lambda a, b: a or b, "OR2", "or2"),
    "xor2": (("a", "b"), lambda a, b: a != b, "XOR2", "xor2"),
    "inv": (("a",), lambda a: not a, "INV", "inv"),
    "and2b": (("a_n", "b"), lambda a_n, b: (not a_n) and b, "AND2B", "and2b"),
    "mux2": (("a0", "a1", "s"), lambda a0, a1, s: a1 if s else a0, "MUX2_NI", "mux2"),
}
_FF: dict[str, tuple[str, bool, str | None, str]] = {
    # role: (trigger, scan, async control ("clear"/"preset"/None), preferred name)
    "ff": ("POSEDGE", False, None, "dfxtp"),
    "ff_reset": ("POSEDGE", False, "clear", "dfrtp"),
    "ff_reset_negedge": ("NEGEDGE", False, "clear", "dfrtn"),
    "scan_ff": ("POSEDGE", True, None, "sdfxtp"),
    "scan_ff_reset": ("POSEDGE", True, "clear", "sdfrtp"),
    "scan_ff_set": ("POSEDGE", True, "preset", "sdfstp"),
}
ROLES = tuple(_FF) + tuple(_COMB)


def _short(pattern: str) -> str:
    return re.sub(r"_(\*|\d+)$", "", pattern).split("__")[-1].lower()


def _drive(name: str) -> tuple[int, int, str]:
    """Drive strength 1 first (a library's standard size), then the smallest."""
    m = re.search(r"(\d+)\D*$", name.rsplit("_", 1)[-1])
    size = int(m.group(1)) if m else 1 << 30
    return (size != 1, size, name)


def _concrete(pattern: str, entry: Mapping[str, Any]) -> str:
    variants = entry.get("variants")
    if variants:
        return sorted((str(v) for v in variants), key=_drive)[0]
    if "*" not in pattern:
        return pattern  # an exact cell name
    return pattern.replace("*", "1")


def _sized(pattern: str, entry: Mapping[str, Any]) -> bool:
    """A pattern of drive strengths (or one with its variants), not a bare name."""
    return "*" in pattern or bool(entry.get("variants"))


def _library_name(cell_map: Mapping[str, Any]) -> str:
    prefixes = {
        k.split("__")[0]
        for k, e in cell_map.items()
        if "__" in k
        and not k.startswith(("$", "\\"))
        and not (isinstance(e, Mapping) and e.get(LIBRARY_KEY))
    }
    return sorted(prefixes)[0] if len(prefixes) == 1 else "the library"


class LibraryRoles:
    def __init__(self, cell_map: Mapping[str, Any], core: Any, name: str | None = None):
        self.cell_map = cell_map
        self.core = core
        self.name = name or _library_name(cell_map)
        self._cache: dict[str, RoleCell] = {}

    def names(self) -> tuple[str, ...]:
        return ROLES

    def __getitem__(self, role: str) -> RoleCell:
        if role not in self._cache:
            if role in _COMB:
                found = self._comb(role)
            elif role in _FF:
                found = self._ff(role)
            else:
                raise KeyError(role)
            if found is None:
                raise PdkError(f"{self.name} has no cell for FaultFlow's {role} role")
            self._cache[role] = found
        return self._cache[role]

    def _entries(self, node_type: str) -> list[tuple[str, Mapping[str, Any]]]:
        return [
            (k, e)
            for k, e in self.cell_map.items()
            if isinstance(e, Mapping)
            and not k.startswith(("$", "\\"))
            and not e.get("unsupported")
            and not e.get(LIBRARY_KEY)  # an extra library's (resolve)
            and e.get("node_type") == node_type
        ]

    def _comb(self, role: str) -> RoleCell | None:
        logical, fn, gate, short = _COMB[role]
        candidates = [
            (k, e)
            for k, e in self._entries("GATE")
            if len(e.get("inputs", [])) == len(logical)
            and len(e.get("outputs", {})) == 1
        ]
        candidates.sort(
            key=lambda ke: (
                _short(ke[0]) != short,
                not _sized(*ke),
                ke[1].get("gate_type") != gate,
                ke[0],
            )
        )
        for pattern, entry in candidates:
            inputs = [str(p) for p in entry["inputs"]]
            for perm in itertools.permutations(range(len(logical))):
                # Entry input k is logical pin logical[perm[k]].
                if all(
                    bool(
                        self.core.eval_gate(
                            str(entry["gate_type"]),
                            [values[perm[k]] for k in range(len(logical))],
                        )
                    )
                    == fn(*values)
                    for values in itertools.product([False, True], repeat=len(logical))
                ):
                    pins = {logical[perm[k]]: inputs[k] for k in range(len(logical))}
                    (out,) = entry["outputs"].values()
                    pins["out"] = str(out)
                    return RoleCell(_concrete(pattern, entry), pins)
        return None

    def _ff(self, role: str) -> RoleCell | None:
        """The role's flop: one of exactly its shape, else (none in the library) one
        with more controls -- a clear, preset or enable the role has no use for --
        each held inactive (``ties``)."""
        trigger, scan, control, short = _FF[role]
        fits = []
        for pattern, entry in self._entries("FF"):
            ff = entry.get("ff") or {}
            outputs = entry.get("outputs") or {}
            if ff.get("trigger") != trigger or bool(ff.get("scan")) != scan:
                continue
            if control and (control not in ff or ff[control].get("level") != "LOW"):
                continue
            if scan and ff["scan"].get("enable_polarity", "HIGH") != "HIGH":
                continue
            if ff.get("output") not in outputs:
                continue
            ties = {}
            for extra in ("clear", "preset"):
                if extra in ff and extra != control:
                    # Inactive: high for an active-low control.
                    low = ff[extra].get("level") == "LOW"
                    ties[str(ff[extra]["pin"])] = "1" if low else "0"
            if "enable" in ff:
                high = ff["enable"].get("level", "HIGH") == "HIGH"
                ties[str(ff["enable"]["pin"])] = "1" if high else "0"
            fits.append(
                (
                    bool(ties),
                    _short(pattern) != short,
                    not _sized(pattern, entry),
                    "QN" in outputs,
                    len(ties),
                    pattern,
                    entry,
                    ties,
                )
            )
        if not fits:
            return None
        *_, pattern, entry, ties = sorted(fits, key=lambda f: f[:6])[0]
        ff = entry["ff"]
        pins = {"clk": str(ff["clock"]), "d": str(ff["data"])}
        if scan:
            pins["si"] = str(ff["scan"]["in"])
            pins["se"] = str(ff["scan"]["enable"])
        if control == "clear":
            pins["reset_n"] = str(ff["clear"]["pin"])
        if control == "preset":
            pins["set_n"] = str(ff["preset"]["pin"])
        pins["q"] = str(entry["outputs"][ff["output"]])
        return RoleCell(_concrete(pattern, entry), pins, ties)


def library_roles(
    cell_map: Mapping[str, Any], *, name: str | None = None
) -> LibraryRoles:
    """The roles of the library ``cell_map`` (a loaded cell map) describes."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    if core is None:
        raise PdkError("finding a library's cells needs the C++ core")
    return LibraryRoles(cell_map, core, name)


# A cell FaultFlow adds names its pins by role (faultflow.pdk.roles); this attribute
# maps the role's logical pins to the cell's own -- set only where they differ from
# Sky130 HD's names, which code reading those cells assumes otherwise (cell_pin).
PINS_ATTR = "faultflow_pins"
HD_PINS: dict[str, dict[str, str]] = {
    "and2": {"a": "A", "b": "B", "out": "X"},
    "or2": {"a": "A", "b": "B", "out": "X"},
    "xor2": {"a": "A", "b": "B", "out": "X"},
    "inv": {"a": "A", "out": "Y"},
    "and2b": {"a_n": "A_N", "b": "B", "out": "X"},
    "mux2": {"a0": "A0", "a1": "A1", "s": "S", "out": "X"},
    "ff": {"clk": "CLK", "d": "D", "q": "Q"},
    "ff_reset": {"clk": "CLK", "d": "D", "reset_n": "RESET_B", "q": "Q"},
    "ff_reset_negedge": {"clk": "CLK_N", "d": "D", "reset_n": "RESET_B", "q": "Q"},
    "scan_ff": {"clk": "CLK", "d": "D", "si": "SCD", "se": "SCE", "q": "Q"},
    "scan_ff_reset": {
        "clk": "CLK",
        "d": "D",
        "si": "SCD",
        "se": "SCE",
        "reset_n": "RESET_B",
        "q": "Q",
    },
    "scan_ff_set": {
        "clk": "CLK",
        "d": "D",
        "si": "SCD",
        "se": "SCE",
        "set_n": "SET_B",
        "q": "Q",
    },
}

_DEFAULT: list[LibraryRoles] = []


def default_roles() -> LibraryRoles:
    """Sky130 HD's roles (the shipped HD cell map): what FaultFlow instantiates when
    no library is given."""
    if not _DEFAULT:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "cells/sky130/sky130_fd_sc_hd.json"
        _DEFAULT.append(library_roles(json.loads(path.read_text(encoding="utf-8"))))
    return _DEFAULT[0]


def role_cell(
    roles: LibraryRoles,
    role: str,
    attributes: Mapping[str, str],
    inputs: Mapping[str, int | str],
    outputs: Mapping[str, int],
) -> dict[str, Any]:
    """A Yosys-JSON cell of the library's ``role`` cell, its logical ``inputs`` and
    ``outputs`` (``{"a0": net, ...}``, ``{"out": net}``) on its own pins."""
    found = roles[role]
    pins = dict(found.pins)
    attrs = dict(attributes)
    used = [*inputs, *outputs]
    if any(pins[name] != HD_PINS[role][name] for name in used):
        attrs[PINS_ATTR] = " ".join(f"{name}={pins[name]}" for name in used)
    return {
        "hide_name": 0,
        "type": found.cell,
        "parameters": {},
        "attributes": attrs,
        "port_directions": {
            **{pins[name]: "input" for name in inputs},
            **{pin: "input" for pin in found.ties},
            **{pins[name]: "output" for name in outputs},
        },
        "connections": {
            **{pins[name]: [net] for name, net in {**inputs, **outputs}.items()},
            **{pin: [value] for pin, value in found.ties.items()},
        },
    }


def cell_pin(cell: Mapping[str, Any], logical: str, default: str) -> str:
    """The pin of ``cell`` (one FaultFlow added by role) that is its role's
    ``logical`` pin: ``default`` (the Sky130 HD name) unless the cell says otherwise."""
    raw = (cell.get("attributes") or {}).get(PINS_ATTR)
    if raw:
        for item in str(raw).split():
            name, _, pin = item.partition("=")
            if name == logical:
                return pin
    return default
