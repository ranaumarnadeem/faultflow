"""Edits to the user's elaborated design (Yosys JSON) that put a shell in place
of a memory: find the instance, check its pins, give the path its own module
copies, swap the macro for the shell, and thread the test ports through every
module on the way."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from faultflow.config import ConfigError
from faultflow.mbist.autombist_config import CollarConfig
from faultflow.mbist.memories import analyze_pins
from faultflow.mbist.shell import CollarPort
from faultflow.mbist.spec import MemorySpec
from faultflow.wrap.ports import _next_id

RESET_PORT = "mbist_rst_n"


class InsertError(ConfigError):
    """The design, or what the insertion file says about it, can't be edited
    the way mbist-insert needs."""


Modules = dict[str, dict[str, Any]]


def _param(value: int) -> str:
    return format(value, "032b")


def _bits(module: dict[str, Any], count: int) -> list[int]:
    start = _next_id(module)
    return list(range(start, start + count))


def _add_net(module: dict[str, Any], name: str, bits: list[Any]) -> None:
    module.setdefault("netnames", {})[name] = {
        "hide_name": 0,
        "bits": list(bits),
        "attributes": {},
    }


def instances_of(modules: Modules, top: str) -> dict[str, int]:
    """How many times each module is instantiated under `top`, counting every
    instance of every module that contains it."""
    counts: dict[str, int] = {}

    def walk(name: str, stack: tuple[str, ...]) -> None:
        for cell in modules[name].get("cells", {}).values():
            ctype = cell.get("type", "")
            if ctype in modules:
                counts[ctype] = counts.get(ctype, 0) + 1
                if ctype not in stack and not _is_blackbox(modules[ctype]):
                    walk(ctype, stack + (ctype,))

    walk(top, (top,))
    return counts


def _is_blackbox(module: dict[str, Any]) -> bool:
    value = module.get("attributes", {}).get("blackbox")
    return value is not None and str(value).strip("0") != ""


def locate(modules: Modules, top: str, path: str) -> list[tuple[str, str]]:
    """The (module, cell) pairs from `top` down to the instance at `path`, each
    cell inside the module before it. Cell names may contain dots (generate
    blocks), so every way to split the path is tried; it must resolve exactly
    one way."""
    found: list[list[tuple[str, str]]] = []

    def walk(module_name: str, rest: str, chain: list[tuple[str, str]]) -> None:
        for cell_name, cell in modules[module_name].get("cells", {}).items():
            if rest == cell_name:
                found.append(chain + [(module_name, cell_name)])
            elif rest.startswith(cell_name + "."):
                sub = cell.get("type", "")
                if sub in modules and not _is_blackbox(modules[sub]):
                    walk(
                        sub,
                        rest[len(cell_name) + 1 :],
                        chain + [(module_name, cell_name)],
                    )

    walk(top, path, [])
    if not found:
        raise InsertError(f"no instance at {path!r} under {top}")
    if len(found) > 1:
        raise InsertError(f"{path!r} names more than one instance under {top}")
    return found[0]


def verify_pins(
    modules: Modules,
    parent: str,
    cell_name: str,
    memory: MemorySpec,
    collar: CollarConfig,
) -> None:
    """Every pin of the memory instance plays a collar role or is declared in
    the insertion file, and every declaration matches the design: a tie pin is
    driven by exactly that constant, a share_clock pin is on the clock pin's
    net, an unused output is read by nothing."""
    cell = modules[parent]["cells"][cell_name]
    if cell.get("type") != collar.memory_name:
        raise InsertError(
            f"memory {memory.name!r}: {memory.instance} is a {cell.get('type')}, "
            f"but its autoMBIST config is for {collar.memory_name}"
        )
    macro = modules[collar.memory_name]
    pins = {p.pin: p for p in analyze_pins(modules[parent], cell_name, macro)}
    where = f"memory {memory.name!r} ({memory.instance})"
    missing = sorted(collar.role_pins - set(pins))
    if missing:
        raise InsertError(
            f"{where}: the macro has no pin(s) {missing} its config names"
        )
    declared = (
        {pin for pin, _ in memory.tie}
        | set(memory.share_clock)
        | set(memory.unused_outputs)
    )
    unknown = sorted(declared - set(pins))
    if unknown:
        raise InsertError(f"{where}: the macro has no pin(s) {unknown}")
    roles = sorted(declared & collar.role_pins)
    if roles:
        raise InsertError(
            f"{where}: {roles} play a collar role (the config's ports); the collar "
            "drives them, so they can't be tied, shared or left unused"
        )
    undeclared = sorted(set(pins) - collar.role_pins - declared)
    if undeclared:
        raise InsertError(
            f"{where}: pin(s) {undeclared} have no collar role and aren't declared "
            "in the insertion file (tie, share_clock or unused_outputs)"
        )
    for pin, value in memory.tie:
        use = pins[pin]
        if use.direction != "input":
            raise InsertError(f"{where}: tie pin {pin} is an {use.direction}")
        if value >= 1 << use.width:
            raise InsertError(
                f"{where}: tie {pin}={value} doesn't fit {use.width} bits"
            )
        if use.constant is None:
            raise InsertError(
                f"{where}: tie {pin}={value}, but the design drives it with "
                f"{use.describe()}"
            )
        if use.constant != value:
            raise InsertError(
                f"{where}: tie {pin}={value}, but the design ties it to {use.constant}"
            )
    clock = collar.pins["clk"]
    for pin in memory.share_clock:
        use = pins[pin]
        if use.direction != "input" or clock not in use.same_as:
            raise InsertError(
                f"{where}: share_clock pin {pin} isn't on the net of the clock pin "
                f"{clock}: the design drives it with {use.describe()}"
            )
    for pin in memory.unused_outputs:
        use = pins[pin]
        if use.direction != "output" or not (use.unread or use.unconnected):
            raise InsertError(
                f"{where}: unused output {pin} is read in the design ({use.describe()})"
            )


def _module_base(name: str) -> str:
    """A Verilog-identifier base for a copy of `name`: `$paramod\\core\\W=8`
    and `$paramod$<hash>\\core` give `core`."""
    if name.startswith("$paramod"):
        parts = name.split("\\")
        if len(parts) > 1 and parts[1]:
            name = parts[1]
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "m"


def copy_name(module: str, path: str) -> str:
    """The name of the copy of `module` instantiated at `path`."""
    where = re.sub(r"[^A-Za-z0-9_]+", "_", path).strip("_")
    return f"{_module_base(module)}__mbist_{where}"


def uniquify_path(
    modules: Modules,
    top: str,
    chain: Sequence[tuple[str, str]],
    taken: Iterable[str] = (),
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Give every module on `chain` below the top its own copy when it is
    instantiated more than once, so editing it for this path changes nothing
    elsewhere. Returns the chain with the copies' names, and copy -> original.
    A copy's name must be new among the design's modules and `taken` (liberty
    cells, the modules mbist-insert adds)."""
    taken_names = set(taken)
    chain = list(chain)
    made: dict[str, str] = {}
    prefix = ""
    for i in range(len(chain) - 1):
        parent, cell_name = chain[i]
        prefix = f"{prefix}.{cell_name}" if prefix else cell_name
        child = modules[parent]["cells"][cell_name]["type"]
        if instances_of(modules, top).get(child, 0) > 1:
            name = copy_name(child, prefix)
            if name in modules or name in taken_names:
                raise InsertError(
                    f"the copy of {child} for {prefix} would be named {name}, but a "
                    "module or cell with that name exists"
                )
            modules[name] = copy.deepcopy(modules[child])
            modules[name].get("attributes", {}).pop("top", None)
            modules[parent]["cells"][cell_name]["type"] = name
            made[name] = child
            child = name
        chain[i + 1] = (child, chain[i + 1][1])
    return chain, made


@dataclass(frozen=True)
class Threaded:
    """The test ports one memory got: the top ports (name -> port) and the
    shell instance's path."""

    memory: str
    shell_path: str
    top_ports: dict[str, str]  # collar port -> top port


def _connect_port(
    module: dict[str, Any], name: str, direction: str, width: int
) -> list[int]:
    if name in module.get("ports", {}) or name in module.get("netnames", {}):
        raise InsertError(f"module already has a port or wire named {name}")
    bits = _bits(module, width)
    module.setdefault("ports", {})[name] = {"direction": direction, "bits": bits}
    _add_net(module, name, bits)
    return bits


def _not_cell(module: dict[str, Any], name: str, a: list[Any]) -> list[int]:
    y = _bits(module, len(a))
    module["cells"][name] = {
        "hide_name": 1,
        "type": "$not",
        "parameters": {
            "A_SIGNED": _param(0),
            "A_WIDTH": _param(len(a)),
            "Y_WIDTH": _param(len(a)),
        },
        "attributes": {},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": list(a), "Y": y},
    }
    _add_net(module, name.lstrip("$").replace("$", "_") + "_y", y)
    return y


def _reset_bits(
    modules: Modules,
    chain: Sequence[tuple[str, str]],
    reset_port: str,
    active_low: bool,
) -> list[Any]:
    """The active-low chip reset in the memory's parent module, threaded down
    from the top as mbist_rst_n through every module on the way."""
    top_name = chain[0][0]
    top = modules[top_name]
    port = top.get("ports", {}).get(reset_port)
    if port is None or port.get("direction") != "input" or len(port["bits"]) != 1:
        raise InsertError(f"the top has no single-bit reset input {reset_port}")
    if RESET_PORT not in top.get("netnames", {}):
        bits = list(port["bits"])
        if not active_low:
            bits = _not_cell(top, "$mbist_reset_not", bits)
        _add_net(top, RESET_PORT, bits)
    bits = top["netnames"][RESET_PORT]["bits"]
    for i in range(1, len(chain)):
        module_name = chain[i][0]
        module = modules[module_name]
        parent, cell_name = chain[i - 1]
        if RESET_PORT not in module.get("ports", {}):
            _connect_port(module, RESET_PORT, "input", 1)
        cell = modules[parent]["cells"][cell_name]
        cell["connections"][RESET_PORT] = list(bits)
        cell.setdefault("port_directions", {})[RESET_PORT] = "input"
        bits = module["ports"][RESET_PORT]["bits"]
    return list(bits)


def swap_and_thread(
    modules: Modules,
    chain: Sequence[tuple[str, str]],
    memory: MemorySpec,
    collar: CollarConfig,
    shell: str,
    ports: Sequence[CollarPort],
    *,
    reset_port: str,
    reset_active_low: bool,
) -> Threaded:
    """Replace the memory instance (the last cell of `chain`) by an instance of
    `shell` of the same name, wired to the macro's role pins, and thread the
    shell's control and status ports up to the top as `<memory>_<port>` (through
    `mbist_<memory>_<port>` on every module between) and the chip reset down."""
    parent_name, cell_name = chain[-1]
    parent = modules[parent_name]
    macro_cell = parent["cells"][cell_name]
    conns = macro_cell["connections"]
    widths = {p.name: p.width for p in ports}
    role = collar.pins
    binding: dict[str, list[Any]] = {
        "clk": list(conns.get(role["clk"], [])),
        "func_csb": list(conns.get(role["csb"], [])),
        "func_addr": list(conns.get(role["addr"], [])),
        "func_din": list(conns.get(role["din"], [])),
        "func_dout": list(conns.get(role["dout"], [])),
    }
    we = list(conns.get(role["we"], []))
    binding["func_we"] = (
        _not_cell(parent, f"$mbist_{memory.name}_we_not", we)
        if collar.we_active_low
        else we
    )
    for name, bits in binding.items():
        if name not in widths:
            raise InsertError(f"the collar has no port {name}")
        if len(bits) != widths[name]:
            raise InsertError(
                f"memory {memory.name!r}: the collar's {name} is {widths[name]} bits, "
                f"the macro pin feeding it {len(bits)}"
            )
    binding["rst_n"] = _reset_bits(modules, chain, reset_port, reset_active_low)

    top_ports: dict[str, str] = {}
    depth = len(chain) - 1  # modules between the top and the shell's parent
    for port in (p for p in ports if p.kind in ("control", "status", "done")):
        direction = "input" if port.kind == "control" else "output"
        top_port = f"{memory.name}_{port.name}"
        inner = f"mbist_{memory.name}_{port.name}"
        # A port on the shell's parent: the top port itself, or `inner`.
        bits = _connect_port(
            modules[chain[depth][0]],
            top_port if depth == 0 else inner,
            direction,
            port.width,
        )
        # Then up, level by level: the cell chain[i - 1] instantiates the module
        # of chain[i], whose `inner` port now exists; give the module holding
        # that cell its own port (the top's under the top-port name) and wire
        # the cell to it.
        for i in range(depth, 0, -1):
            holder = modules[chain[i - 1][0]]
            up_bits = _connect_port(
                holder, top_port if i == 1 else inner, direction, port.width
            )
            cell = holder["cells"][chain[i - 1][1]]
            cell["connections"][inner] = list(up_bits)
            cell.setdefault("port_directions", {})[inner] = direction
        binding[port.name] = bits
        top_ports[port.name] = top_port

    missing = [p.name for p in ports if p.name not in binding]
    if missing:
        raise InsertError(f"the shell's port(s) {missing} have nothing to connect to")
    parent["cells"][cell_name] = {
        "hide_name": 0,
        "type": shell,
        "parameters": {},
        "attributes": {},
        "port_directions": {p.name: p.direction for p in ports},
        "connections": {p.name: list(binding[p.name]) for p in ports},
    }
    path = ".".join(cell for _, cell in chain)
    return Threaded(memory=memory.name, shell_path=path, top_ports=top_ports)
