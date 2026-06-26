#!/usr/bin/env python3
"""
Inject IEEE 1500 wrapper cells ($wbc_in_faultflow / $wbc_out_faultflow) onto
every input and output port of a synthesized Yosys JSON netlist.

Usage:
    python3 scripts/wrap_ports.py input.json output.json [--top TOP_MODULE]
"""

import argparse
import copy
import json
import sys


def wrap(src: dict, top: str | None) -> dict:
    dst = copy.deepcopy(src)
    modules = dst["modules"]

    if top is None:
        # pick the module with the top attribute
        for name, mod in modules.items():
            attrs = mod.get("attributes", {})
            if attrs.get("top") in ("1", "00000000000000000000000000000001", 1):
                top = name
                break
        if top is None:
            top = next(iter(modules))

    mod = modules[top]
    ports = mod["ports"]
    cells = mod.setdefault("cells", {})
    netnames = mod.setdefault("netnames", {})

    # find next free net id
    used = set()
    for p in ports.values():
        for b in p["bits"]:
            if isinstance(b, int):
                used.add(b)
    for c in cells.values():
        for bits in c.get("connections", {}).values():
            for b in bits:
                if isinstance(b, int):
                    used.add(b)
    for nn in netnames.values():
        for b in nn.get("bits", []):
            if isinstance(b, int):
                used.add(b)

    next_id = max(used) + 1 if used else 2

    def alloc(n: int = 1) -> list[int]:
        nonlocal next_id
        ids = list(range(next_id, next_id + n))
        next_id += n
        return ids

    for port_name, port in list(ports.items()):
        direction = port["direction"]
        bits = port["bits"]
        w = len(bits)

        if direction == "input":
            # insert wbc_in: FROM_SYS = port bit, TO_CORE = new core net
            core_bits = alloc(w)
            for i, (sys_bit, core_bit) in enumerate(zip(bits, core_bits)):
                suffix = "" if w == 1 else f"_{i}"
                cell_name = f"__wi_{port_name}{suffix}"
                cells[cell_name] = {
                    "hide_name": 0,
                    "type": "$wbc_in_faultflow",
                    "parameters": {},
                    "attributes": {},
                    "port_directions": {"FROM_SYS": "input", "TO_CORE": "output"},
                    "connections": {"FROM_SYS": [sys_bit], "TO_CORE": [core_bit]},
                }
                nn_core = f"__core_{port_name}{suffix}"
                netnames[nn_core] = {
                    "hide_name": 0,
                    "bits": [core_bit],
                    "attributes": {},
                }

        elif direction == "output":
            # insert wbc_out: FROM_CORE = port bit (driven by core logic), TO_SYS = new sys net
            # The port now connects to the TO_SYS net
            sys_bits = alloc(w)
            for i, (core_bit, sys_bit) in enumerate(zip(bits, sys_bits)):
                suffix = "" if w == 1 else f"_{i}"
                cell_name = f"__wo_{port_name}{suffix}"
                cells[cell_name] = {
                    "hide_name": 0,
                    "type": "$wbc_out_faultflow",
                    "parameters": {},
                    "attributes": {},
                    "port_directions": {"FROM_CORE": "input", "TO_SYS": "output"},
                    "connections": {"FROM_CORE": [core_bit], "TO_SYS": [sys_bit]},
                }
                nn_sys = f"__sys_{port_name}{suffix}"
                netnames[nn_sys] = {"hide_name": 0, "bits": [sys_bit], "attributes": {}}
            # Redirect the port bits to the new sys nets
            port["bits"] = sys_bits

    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="Input Yosys JSON netlist")
    ap.add_argument("output", help="Output wrapped JSON netlist")
    ap.add_argument("--top", help="Top module name (auto-detected if omitted)")
    args = ap.parse_args()

    src = json.load(open(args.input))
    dst = wrap(src, args.top)
    json.dump(dst, open(args.output, "w"), indent=2)

    # Report
    top = args.top
    if top is None:
        for name, mod in dst["modules"].items():
            if mod.get("attributes", {}).get("top") in (
                "1",
                "00000000000000000000000000000001",
                1,
            ):
                top = name
                break
    mod = dst["modules"][top]
    wbc_in = sum(1 for c in mod["cells"].values() if c["type"] == "$wbc_in_faultflow")
    wbc_out = sum(1 for c in mod["cells"].values() if c["type"] == "$wbc_out_faultflow")
    print(f"Wrapped {top}: {wbc_in} wbc_in + {wbc_out} wbc_out cells inserted")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
