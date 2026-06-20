#!/usr/bin/env python3
"""Parse Liberty by finding function lines near each cell header."""
import re

LIB = "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
lines = open(LIB).readlines()

targets = [
    "a211o_1", "a222oi_1", "a2bb2oi_1", "a311o_1", "a311oi_1", "a41o_1",
    "and3b_1", "and4_1", "and4b_1", "edfxtp_1", "maj3_1", "mux2i_1", "mux4_2",
    "o2111a_1", "o2111ai_1", "o211a_1", "o21ba_1", "o221a_1", "o2bb2ai_1",
    "o41a_1", "o41ai_1", "or4_1", "xor3_1",
]

# Find where each cell starts
cell_starts = {}
for i, ln in enumerate(lines):
    m = re.search(r'cell \("?(sky130_fd_sc_hd__\w+)"?\)', ln)
    if m:
        cell_starts[m.group(1)] = i

for t in targets:
    name = f"sky130_fd_sc_hd__{t}"
    if name not in cell_starts:
        print(f"NOT FOUND: {name}")
        continue
    start = cell_starts[name]
    chunk = lines[start: start + 600]  # ~600 lines per cell
    # Find input pins (direction : "input")
    inp, out = [], {}
    cur_pin = None
    for ln in chunk:
        pm = re.search(r'pin \("?(\w+)"?\)', ln)
        if pm:
            cur_pin = pm.group(1)
        if cur_pin and 'pg_type' not in ln:
            if 'direction : "input"' in ln or "direction : input" in ln:
                if cur_pin not in inp:
                    inp.append(cur_pin)
            # match only bare 'function :' not 'power_down_function :'
            fm = re.search(r'^\s+function\s*:\s*"([^"]+)"', ln)
            if fm and cur_pin not in out:  # first match wins
                out[cur_pin] = fm.group(1)
    # filter power pins
    inp = [p for p in inp if p not in ("VPWR", "VGND", "VPB", "VNB")]
    print(f"=== {name} ===")
    print(f"  inputs : {inp}")
    for k, v in out.items():
        print(f"  {k} = {v}")
    print()
