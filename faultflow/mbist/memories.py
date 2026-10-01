"""The memory instances of an elaborated design, and how each pin is connected:
what ``ff.py list-memories`` prints to help write an insertion file."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from faultflow.mbist.spec import MbistSpec, load_mbist_spec
from faultflow.mbist.yosys import elaborate


@dataclass(frozen=True)
class PinUse:
    """One pin of a memory instance, in the module that instantiates it."""

    pin: str
    direction: str  # "input" | "output" | "inout"
    width: int
    # Every bit tied to 0 or 1: the value, bit 0 the LSB.
    constant: int | None = None
    # Nothing on the pin, or only x/z bits.
    unconnected: bool = False
    # Other pins of the instance on exactly these bits (a shared clock, say).
    same_as: tuple[str, ...] = ()
    # An output that nothing in the instantiating module reads, nor passes up.
    unread: bool = False
    # A wire carrying exactly these bits, if the module names one.
    net: str | None = None

    def describe(self) -> str:
        if self.unconnected:
            return "unconnected"
        if self.constant is not None:
            return (
                f"constant {self.constant:#x}"
                if self.width > 1
                else (f"constant {self.constant}")
            )
        text = f"net {self.net}" if self.net else "logic"
        if self.same_as:
            text += f" (same net as {', '.join(self.same_as)})"
        if self.unread:
            text = f"unread ({text})"
        return text


@dataclass(frozen=True)
class MemoryInstance:
    path: str  # dot-joined instance names from the top, generate brackets kept
    module: str  # the memory macro
    parent: str  # the module the instance is in
    pins: tuple[PinUse, ...]

    def pin(self, name: str) -> PinUse:
        return next(p for p in self.pins if p.pin == name)


def _is_blackbox(module: dict[str, Any]) -> bool:
    value = module.get("attributes", {}).get("blackbox")
    if value is None:
        return False
    try:
        return int(str(value), 2 if set(str(value)) <= {"0", "1"} else 10) != 0
    except ValueError:
        return bool(value)


def _port_order(module: dict[str, Any]) -> list[str]:
    return list(module.get("ports", {}))


def _analyze(
    parent: dict[str, Any], cell_name: str, macro: dict[str, Any]
) -> tuple[PinUse, ...]:
    cell = parent["cells"][cell_name]
    conns: dict[str, list[Any]] = cell.get("connections", {})
    directions = cell.get("port_directions", {})
    read: set[int] = set()
    for other_name, other in parent.get("cells", {}).items():
        other_dirs = other.get("port_directions", {})
        for pin, bits in other.get("connections", {}).items():
            if other_dirs.get(pin, "input") != "output":
                read.update(b for b in bits if isinstance(b, int))
    for port in parent.get("ports", {}).values():
        if port.get("direction") in ("output", "inout"):
            read.update(b for b in port.get("bits", []) if isinstance(b, int))
    named: dict[tuple[Any, ...], str] = {}
    for name, net in sorted(parent.get("netnames", {}).items()):
        if not net.get("hide_name"):
            named.setdefault(tuple(net.get("bits", [])), name)

    pins = []
    for pin in _port_order(macro):
        port = macro["ports"][pin]
        direction = directions.get(pin, port.get("direction", "input"))
        width = len(port.get("bits", []))
        bits = conns.get(pin, [])
        ints = [b for b in bits if isinstance(b, int)]
        if not ints and all(b in ("x", "z") for b in bits):
            pins.append(PinUse(pin, direction, width, unconnected=True))
            continue
        if not ints and all(b in ("0", "1") for b in bits):
            value = sum(1 << i for i, b in enumerate(bits) if b == "1")
            pins.append(PinUse(pin, direction, width, constant=value))
            continue
        same_as = tuple(
            other
            for other in _port_order(macro)
            if other != pin and conns.get(other) == bits
        )
        pins.append(
            PinUse(
                pin,
                direction,
                width,
                same_as=same_as,
                unread=direction == "output" and not (set(ints) & read),
                net=named.get(tuple(bits)),
            )
        )
    return tuple(pins)


def find_memories(
    netlist: dict[str, Any], top: str, patterns: Sequence[str] = ("*",)
) -> tuple[MemoryInstance, ...]:
    """Every instance of a blackbox module whose name matches one of `patterns`,
    reached from `top`, sorted by path."""
    modules: dict[str, dict[str, Any]] = netlist["modules"]
    if top not in modules:
        raise KeyError(f"the design has no module {top!r}")
    found: list[MemoryInstance] = []
    analyzed: dict[tuple[str, str], tuple[PinUse, ...]] = {}

    def walk(module_name: str, prefix: str, stack: tuple[str, ...]) -> None:
        module = modules[module_name]
        for cell_name, cell in module.get("cells", {}).items():
            ctype = cell.get("type", "")
            sub = modules.get(ctype)
            if sub is None:
                continue
            path = f"{prefix}{cell_name}"
            if _is_blackbox(sub):
                if any(fnmatchcase(ctype, p) for p in patterns):
                    key = (module_name, cell_name)
                    if key not in analyzed:
                        analyzed[key] = _analyze(module, cell_name, sub)
                    found.append(
                        MemoryInstance(path, ctype, module_name, analyzed[key])
                    )
            elif ctype not in stack:
                walk(ctype, f"{path}.", stack + (ctype,))

    walk(top, "", (top,))
    return tuple(sorted(found, key=lambda m: m.path))


def suggested_name(path: str) -> str:
    """An insertion-file memory name for an instance path."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", path).strip("_")
    return name if name and not name[0].isdigit() else f"m_{name}"


def spec_entry(memory: MemoryInstance) -> str:
    """A `memories:` entry for the insertion file, from how the pins are
    connected: constant inputs as `tie`, an input on another input's net as
    `share_clock`, unread outputs as `unused_outputs`. The pins autoMBIST drives
    (its config's `ports`) must come out of these lists."""
    tie = [p for p in memory.pins if p.direction == "input" and p.constant is not None]
    shared = [
        p.pin
        for p in memory.pins
        if p.direction == "input"
        and p.same_as
        and memory.pins.index(p)
        > min(memory.pins.index(memory.pin(o)) for o in p.same_as)
    ]
    unused = [
        p.pin
        for p in memory.pins
        if p.direction == "output" and (p.unread or p.unconnected)
    ]
    ties = ", ".join(
        f"{p.pin}: {p.constant:#x}" if p.width > 1 else f"{p.pin}: {p.constant}"
        for p in tie
    )
    lines = [
        f"  - name: {suggested_name(memory.path)}",
        f'    instance: "{memory.path}"',
        f"    autombist_config: <your autoMBIST config for {memory.module}>",
        "    # drop the pins your autoMBIST config gives a role (its ports:)",
    ]
    if tie:
        lines.append(f"    tie: {{{ties}}}")
    if shared:
        lines.append(f"    share_clock: [{', '.join(shared)}]")
    if unused:
        lines.append(f"    unused_outputs: [{', '.join(unused)}]")
    return "\n".join(lines)


def render_listing(memories: Sequence[MemoryInstance], spec: MbistSpec) -> str:
    """The list-memories report: each memory instance, whether the insertion
    file configures it, its pins, and an entry to paste for those it doesn't."""
    if not memories:
        return (
            "no memory instances: no blackbox module matching "
            f"{', '.join(spec.memory_patterns)} is instantiated under the top"
        )
    out: list[str] = []
    for memory in memories:
        configured = spec.memory_at(memory.path)
        status = (
            f"configured as {configured.name}"
            if configured is not None
            else "not configured"
        )
        out.append(f"{memory.path}  ({memory.module} in {memory.parent})  {status}")
        width = max(len(p.pin) for p in memory.pins)
        for p in memory.pins:
            out.append(
                f"    {p.pin.ljust(width)}  {p.direction:<6}  {p.width:>3}  "
                f"{p.describe()}"
            )
        if configured is None:
            out.append("  insertion-file entry:")
            out.append(spec_entry(memory))
        out.append("")
    configured_paths = {m.instance for m in spec.memories}
    missing = sorted(configured_paths - {m.path for m in memories})
    if missing:
        out.append(
            "configured but not listed (no blackbox instance at that path, or "
            "its module doesn't match memory_patterns): " + ", ".join(missing)
        )
    return "\n".join(out).rstrip() + "\n"


def list_memories(
    spec_path: str | Path, top: str, patterns: Sequence[str] | None = None
) -> str:
    """The list-memories report for the insertion file's design under `top`;
    `patterns` replace the file's memory_patterns. Yosys works in a temporary
    directory, kept only if it fails (the error names its log)."""
    spec = load_mbist_spec(spec_path)
    if patterns:
        spec = replace(spec, memory_patterns=tuple(patterns))
    work = Path(tempfile.mkdtemp(prefix="ff_list_memories_"))
    elab = elaborate(spec.design, top, workdir=work)
    shutil.rmtree(work, ignore_errors=True)
    memories = find_memories(elab.netlist, top, spec.memory_patterns)
    return render_listing(memories, spec)
