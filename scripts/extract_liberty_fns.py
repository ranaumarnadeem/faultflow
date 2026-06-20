#!/usr/bin/env python3
"""Extract Liberty pin functions for missing Sky130 cells."""
import re
import sys

LIB = "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

targets = [
    "a211o_1", "a222oi_1", "a2bb2oi_1", "a311o_1", "a311oi_1", "a41o_1",
    "and3b_1", "and4_1", "and4b_1", "edfxtp_1", "maj3_1", "mux2i_1", "mux4_2",
    "o2111a_1", "o2111ai_1", "o211a_1", "o21ba_1", "o221a_1", "o2bb2ai_1",
    "o41a_1", "o41ai_1", "or4_1", "xor3_1",
]

lib = open(LIB).read()

for t in targets:
    pat = rf'cell\s*\("?sky130_fd_sc_hd__{re.escape(t)}"?\)'
    m = re.search(pat, lib)
    if not m:
        print(f"NOT FOUND: {t}")
        continue
    chunk = lib[m.start(): m.start() + 2000]

    # input pin names
    inp = re.findall(
        r'pin\s*\("?(\w+)"?\)\s*\{[^}]*?direction\s*:\s*input', chunk, re.DOTALL
    )
    # output pin -> function
    out = re.findall(
        r'pin\s*\("?(\w+)"?\)\s*\{[^}]*?direction\s*:\s*output[^}]*?'
        r'function\s*:\s*"([^"]+)"',
        chunk,
        re.DOTALL,
    )
    print(f"=== sky130_fd_sc_hd__{t} ===")
    print(f"  inputs : {inp}")
    for pname, fn in out:
        print(f"  {pname} = {fn}")
    print()
