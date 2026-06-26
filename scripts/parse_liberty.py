#!/usr/bin/env python3
"""Parse Liberty to extract pin functions for missing Sky130 cells."""

import re

LIB = "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
lib = open(LIB).read()
lines = lib.split("\n")

# Build line-offset index
offsets = []
pos = 0
for ln in lines:
    offsets.append(pos)
    pos += len(ln) + 1

# Find all cell start positions
cell_pos = {}
for i, ln in enumerate(lines):
    m = re.search(r'cell \("?(sky130_fd_sc_hd__\w+)"?\)', ln)
    if m:
        cell_pos[m.group(1)] = offsets[i]

targets = [
    "sky130_fd_sc_hd__a211o_1",
    "sky130_fd_sc_hd__a222oi_1",
    "sky130_fd_sc_hd__a2bb2oi_1",
    "sky130_fd_sc_hd__a311o_1",
    "sky130_fd_sc_hd__a311oi_1",
    "sky130_fd_sc_hd__a41o_1",
    "sky130_fd_sc_hd__and3b_1",
    "sky130_fd_sc_hd__and4_1",
    "sky130_fd_sc_hd__and4b_1",
    "sky130_fd_sc_hd__edfxtp_1",
    "sky130_fd_sc_hd__maj3_1",
    "sky130_fd_sc_hd__mux2i_1",
    "sky130_fd_sc_hd__mux4_2",
    "sky130_fd_sc_hd__o2111a_1",
    "sky130_fd_sc_hd__o2111ai_1",
    "sky130_fd_sc_hd__o211a_1",
    "sky130_fd_sc_hd__o21ba_1",
    "sky130_fd_sc_hd__o221a_1",
    "sky130_fd_sc_hd__o2bb2ai_1",
    "sky130_fd_sc_hd__o41a_1",
    "sky130_fd_sc_hd__o41ai_1",
    "sky130_fd_sc_hd__or4_1",
    "sky130_fd_sc_hd__xor3_1",
]

for name in targets:
    if name not in cell_pos:
        print(f"NOT FOUND: {name}")
        continue
    chunk = lib[cell_pos[name] : cell_pos[name] + 4000]
    # Simple: find all pin(...) blocks
    inp, out = [], {}
    for pm in re.finditer(r"pin \((\w+)\) \{([^}]+)\}", chunk, re.DOTALL):
        pname, body = pm.group(1), pm.group(2)
        if "pg_type" in body:
            continue
        if "direction : input" in body:
            inp.append(pname)
        if "direction : output" in body:
            fn = re.search(r'function : "([^"]+)"', body)
            if fn:
                out[pname] = fn.group(1)
    print(f"{name}:")
    print(f"  inputs : {inp}")
    for k, v in out.items():
        print(f"  {k} = {v}")
    print()
