"""Structural netlist facts for DFT rules, derived from the Yosys JSON + cell map.

Reuses the scan layer's cell-map glob matching and JSON loaders so a rule sees
exactly the same cell semantics the rest of faultflow does. Single-bit pin
connections only (the supported gate/FF cells are scalar); constants ("0"/"1"/
"x"/"z") and multi-bit buses are ignored for graph purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.scan.stitch import _load_json, _lookup_cell, _top_module


@dataclass(frozen=True)
class CellFact:
    instance: str
    cell_type: str
    node_type: str  # GATE | FF | CONST | LATCH | TBUF | ICG
    gate_type: str | None  # e.g. BUF/INV/AND2 (GATE/CONST only)
    inputs: dict[str, int]  # physical input pin -> net id
    outputs: dict[str, int]  # physical output pin -> net id
    clock_pin: str | None  # FF clock pin name, if FF
    clock_net: int | None  # net on the FF clock pin

    @property
    def is_ff(self) -> bool:
        return self.node_type == "FF"

    @property
    def is_buffer(self) -> bool:
        # Pure single-input pass/invert cell -> transparent for clock control.
        return self.node_type == "GATE" and self.gate_type in {"BUF", "INV"}


@dataclass
class NetlistFacts:
    top: str
    pis: set[int] = field(default_factory=set)
    pos: set[int] = field(default_factory=set)
    cells: list[CellFact] = field(default_factory=list)
    driver_of: dict[int, tuple[str, str]] = field(default_factory=dict)
    sinks_of: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    clock_nets: set[int] = field(default_factory=set)
    multi_driven: dict[int, list[str]] = field(default_factory=dict)
    _net_names: dict[int, str] = field(default_factory=dict)

    def net_name(self, net: int) -> str:
        return self._net_names.get(net, f"net{net}")


def _one_int_bit(value: Any) -> int | None:
    if not isinstance(value, list) or len(value) != 1:
        return None
    bit = value[0]
    return bit if isinstance(bit, int) else None


def _port_bits(port: Any) -> list[int]:
    if not isinstance(port, dict):
        return []
    bits = port.get("bits")
    if not isinstance(bits, list):
        return []
    return [b for b in bits if isinstance(b, int)]


def build_netlist_facts(
    netlist_json: Path, cell_map_json: Path, top: str
) -> NetlistFacts:
    data = _load_json(netlist_json)
    cell_map = _load_json(cell_map_json)
    _, module = _top_module(data, top)
    facts = NetlistFacts(top=top)

    ports = module.get("ports", {})
    if isinstance(ports, dict):
        for name, port in ports.items():
            if not isinstance(port, dict):
                continue
            direction = port.get("direction")
            for bit in _port_bits(port):
                facts._net_names.setdefault(bit, str(name))
                if direction == "input":
                    facts.pis.add(bit)
                elif direction == "output":
                    facts.pos.add(bit)

    netnames = module.get("netnames", {})
    if isinstance(netnames, dict):
        for name, net in netnames.items():
            for bit in _port_bits(net):
                facts._net_names.setdefault(bit, str(name))

    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return facts

    for instance, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("type", ""))
        match = _lookup_cell(cell_map, cell_type)
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            conns = {}
        node_type = "GATE"
        gate_type: str | None = None
        in_pins: list[str] = []
        out_pins: list[str] = []
        clock_pin: str | None = None
        if match is not None:
            _, entry = match
            node_type = str(entry.get("node_type", "GATE"))
            gt = entry.get("gate_type")
            gate_type = str(gt) if isinstance(gt, str) else None
            in_pins = [str(p) for p in entry.get("inputs", []) if isinstance(p, str)]
            outs = entry.get("outputs", {})
            if isinstance(outs, dict):
                out_pins = [str(v) for v in outs.values() if isinstance(v, str)]
            ff_meta = entry.get("ff")
            if node_type == "FF" and isinstance(ff_meta, dict):
                cp = ff_meta.get("clock")
                clock_pin = str(cp) if isinstance(cp, str) and cp else None

        input_nets: dict[str, int] = {}
        for pin in in_pins:
            net_bit = _one_int_bit(conns.get(pin))
            if net_bit is not None:
                input_nets[pin] = net_bit
        output_nets: dict[str, int] = {}
        for pin in out_pins:
            net_bit = _one_int_bit(conns.get(pin))
            if net_bit is not None:
                output_nets[pin] = net_bit

        clock_net: int | None = None
        if clock_pin is not None:
            clock_net = input_nets.get(clock_pin)
            if clock_net is not None:
                facts.clock_nets.add(clock_net)

        fact = CellFact(
            instance=str(instance),
            cell_type=cell_type,
            node_type=node_type,
            gate_type=gate_type,
            inputs=input_nets,
            outputs=output_nets,
            clock_pin=clock_pin,
            clock_net=clock_net,
        )
        facts.cells.append(fact)

        for pin, net in output_nets.items():
            if net in facts.driver_of:
                prior = facts.driver_of[net][0]
                facts.multi_driven.setdefault(net, [prior]).append(fact.instance)
            else:
                facts.driver_of[net] = (fact.instance, pin)
        for pin, net in input_nets.items():
            facts.sinks_of.setdefault(net, []).append((fact.instance, pin))

    return facts
