"""Inject IEEE 1500 wrapper cells onto a Yosys-JSON netlist's boundary ports.

The ``buffer`` model reproduces ``scripts/wrap_ports.py`` byte-for-byte (transparent
``$wbc_*_faultflow`` cells). The ``scan`` model emits native shiftable
``$wbc_*_scan_faultflow`` cells and stitches their boundary registers into one
dedicated wrapper scan chain (head = ``scan_in``, tail = ``scan_out``), sharing the
design clock and a wrapper scan-enable. Each scan wrapper cell is tagged
``faultflow_wbr`` / ``faultflow_wbr_chain`` / ``wbr_bit`` so the stitch + aggregate
layers can identify and order it.
"""

from __future__ import annotations

import copy
from typing import Any

from faultflow.config import ConfigError

_TOP_ATTRS = ("1", "00000000000000000000000000000001", 1)


class WrapError(ConfigError):
    """A netlist that cannot be wrapped (missing top, bad ports, etc.)."""


def _resolve_top(modules: dict[str, Any], top: str | None) -> str:
    if top is not None:
        if top not in modules:
            raise WrapError(f"top module {top!r} not found in netlist")
        return top
    for name, mod in modules.items():
        if mod.get("attributes", {}).get("top") in _TOP_ATTRS:
            return name
    if not modules:
        raise WrapError("netlist has no modules")
    return next(iter(modules))


def _next_id(mod: dict[str, Any]) -> int:
    used: set[int] = set()
    for p in mod.get("ports", {}).values():
        used.update(b for b in p.get("bits", []) if isinstance(b, int))
    for c in mod.get("cells", {}).values():
        for bits in c.get("connections", {}).values():
            used.update(b for b in bits if isinstance(b, int))
    for nn in mod.get("netnames", {}).values():
        used.update(b for b in nn.get("bits", []) if isinstance(b, int))
    return max(used) + 1 if used else 2


def _net(bit: int) -> dict[str, Any]:
    return {"hide_name": 0, "bits": [bit], "attributes": {}}


def wrap_ports(
    src: dict[str, Any],
    top: str | None = None,
    *,
    wbr_model: str = "buffer",
    targets: list[str] | None = None,
    scan_in: str = "wbr_si",
    scan_out: str = "wbr_so",
    scan_enable: str = "wbr_se",
    clock: str = "CLK",
) -> dict[str, Any]:
    """Return a deep-copied netlist with wrapper cells on the boundary ports.

    ``targets`` optionally restricts wrapping to a subset of port names (default:
    every port). ``scan`` model adds the wrapper scan-chain ports if absent.
    """
    if wbr_model not in {"buffer", "scan"}:
        raise WrapError("wbr_model must be 'buffer' or 'scan'")
    dst = copy.deepcopy(src)
    modules = dst.get("modules")
    if not isinstance(modules, dict):
        raise WrapError("netlist has no 'modules' object")
    top = _resolve_top(modules, top)
    mod = modules[top]
    ports = mod.setdefault("ports", {})
    cells = mod.setdefault("cells", {})
    netnames = mod.setdefault("netnames", {})

    next_id = _next_id(mod)

    def alloc(n: int = 1) -> list[int]:
        nonlocal next_id
        ids = list(range(next_id, next_id + n))
        next_id += n
        return ids

    want = set(targets) if targets is not None else None
    if want is not None:
        missing = [p for p in want if p not in ports]
        if missing:
            raise WrapError(f"target ports not found: {sorted(missing)}")

    # Snapshot the ORIGINAL core cells: an input wrapper's TO_CORE must replace the
    # raw port net at every core consumer (the wrapper goes INTO the signal path);
    # only pre-existing cells are rewired, never the wrapper cells we add below.
    original_cells = list(cells.keys())

    def rewire_input(port_bit: int, core_bit: int) -> None:
        for cname in original_cells:
            conns = cells[cname].get("connections", {})
            for pin, pin_bits in conns.items():
                conns[pin] = [core_bit if b == port_bit else b for b in pin_bits]

    # scan model shares one clock + scan-enable; the chain is daisied below.
    scan = wbr_model == "scan"
    clk_bit = se_bit = None
    chain_prev: int | None = None  # previous cell's CTO net (chain link)
    bit_pos = 0
    if scan:
        clk_bit = _ensure_port(ports, netnames, clock, "input", alloc)
        se_bit = _ensure_port(ports, netnames, scan_enable, "input", alloc)
        chain_prev = _ensure_port(ports, netnames, scan_in, "input", alloc)

    for port_name, port in list(ports.items()):
        if scan and port_name in {clock, scan_enable, scan_in, scan_out}:
            continue
        if want is not None and port_name not in want:
            continue
        direction = port.get("direction")
        bits = port.get("bits", [])
        width = len(bits)
        for i, bit in enumerate(bits):
            suffix = "" if width == 1 else f"_{i}"
            if direction == "input":
                core_bit = alloc()[0]
                netnames[f"__core_{port_name}{suffix}"] = _net(core_bit)
                rewire_input(bit, core_bit)
                if scan:
                    assert clk_bit is not None and se_bit is not None
                    assert chain_prev is not None
                    cto = alloc()[0]
                    cells[f"__wi_{port_name}{suffix}"] = _scan_cell(
                        "$wbc_in_scan_faultflow",
                        "input",
                        bit_pos,
                        {
                            "CLK": clk_bit,
                            "FROM_SYS": bit,
                            "CTI": chain_prev,
                            "SE": se_bit,
                            "TO_CORE": core_bit,
                            "CTO": cto,
                        },
                    )
                    netnames[f"__cto_{port_name}{suffix}"] = _net(cto)
                    chain_prev = cto
                    bit_pos += 1
                else:
                    cells[f"__wi_{port_name}{suffix}"] = _buffer_cell(
                        "$wbc_in_faultflow",
                        {"FROM_SYS": "input", "TO_CORE": "output"},
                        {"FROM_SYS": [bit], "TO_CORE": [core_bit]},
                    )
            elif direction == "output":
                sys_bit = alloc()[0]
                netnames[f"__sys_{port_name}{suffix}"] = _net(sys_bit)
                if scan:
                    assert clk_bit is not None and se_bit is not None
                    assert chain_prev is not None
                    cto = alloc()[0]
                    cells[f"__wo_{port_name}{suffix}"] = _scan_cell(
                        "$wbc_out_scan_faultflow",
                        "output",
                        bit_pos,
                        {
                            "CLK": clk_bit,
                            "FROM_CORE": bit,
                            "CTI": chain_prev,
                            "SE": se_bit,
                            "TO_SYS": sys_bit,
                            "CTO": cto,
                        },
                    )
                    netnames[f"__cto_{port_name}{suffix}"] = _net(cto)
                    chain_prev = cto
                    bit_pos += 1
                else:
                    cells[f"__wo_{port_name}{suffix}"] = _buffer_cell(
                        "$wbc_out_faultflow",
                        {"FROM_CORE": "input", "TO_SYS": "output"},
                        {"FROM_CORE": [bit], "TO_SYS": [sys_bit]},
                    )
                port["bits"] = (
                    [sys_bit] if width == 1 else _replace_bit(port["bits"], i, sys_bit)
                )

    if scan:
        # Tail of the daisy chain becomes the wrapper scan-out port.
        if bit_pos == 0:
            raise WrapError("scan wrap produced no wrapper cells (no boundary ports)")
        ports[scan_out] = {"direction": "output", "bits": [chain_prev]}
        netnames.setdefault(scan_out, _net(chain_prev))  # type: ignore[arg-type]

    return dst


def _replace_bit(bits: list[Any], i: int, new: int) -> list[Any]:
    out = list(bits)
    out[i] = new
    return out


def _ensure_port(
    ports: dict[str, Any],
    netnames: dict[str, Any],
    name: str,
    direction: str,
    alloc: Any,
) -> int:
    """Return the (single-bit) net of an existing or newly-created port."""
    existing = ports.get(name)
    if isinstance(existing, dict) and existing.get("bits"):
        bit = existing["bits"][0]
        if not isinstance(bit, int):
            raise WrapError(f"port {name!r} has a non-integer bit")
        return bit
    bit = alloc()[0]
    ports[name] = {"direction": direction, "bits": [bit]}
    netnames.setdefault(name, _net(bit))
    return bit


def _buffer_cell(
    ctype: str, dirs: dict[str, str], conns: dict[str, list[int]]
) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": ctype,
        "parameters": {},
        "attributes": {},
        "port_directions": dirs,
        "connections": conns,
    }


def _scan_cell(
    ctype: str, side: str, bit_pos: int, conns: dict[str, int]
) -> dict[str, Any]:
    if side == "input":
        dirs = {
            "CLK": "input",
            "FROM_SYS": "input",
            "CTI": "input",
            "SE": "input",
            "TO_CORE": "output",
            "CTO": "output",
        }
    else:
        dirs = {
            "CLK": "input",
            "FROM_CORE": "input",
            "CTI": "input",
            "SE": "input",
            "TO_SYS": "output",
            "CTO": "output",
        }
    return {
        "hide_name": 0,
        "type": ctype,
        "parameters": {},
        "attributes": {
            "faultflow_wbr": side,
            "faultflow_wbr_chain": "0",
            "wbr_bit": str(bit_pos),
        },
        "port_directions": dirs,
        "connections": {pin: [net] for pin, net in conns.items()},
    }
