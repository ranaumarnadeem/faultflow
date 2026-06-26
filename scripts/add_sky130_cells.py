#!/usr/bin/env python3
"""Add missing Sky130 cell entries to sky130_fd_sc_hd.json."""

import json
import sys

PATH = "cells/sky130/sky130_fd_sc_hd.json"
data = json.load(open(PATH))

new_entries = {
    # --- simple direct GateType mappings ---
    "sky130_fd_sc_hd__and4_*": {
        "node_type": "GATE",
        "gate_type": "AND4",
        "inputs": ["A", "B", "C", "D"],
        "outputs": {"X": "X"},
    },
    "sky130_fd_sc_hd__or4_*": {
        "node_type": "GATE",
        "gate_type": "OR4",
        "inputs": ["A", "B", "C", "D"],
        "outputs": {"X": "X"},
    },
    "sky130_fd_sc_hd__xor3_*": {
        "node_type": "GATE",
        "gate_type": "XOR3",
        "inputs": ["A", "B", "C"],
        "outputs": {"X": "X"},
    },
    # maj3 = ADDF_CO semantics: (A&B)|(A&C)|(B&C)
    "sky130_fd_sc_hd__maj3_*": {
        "node_type": "GATE",
        "gate_type": "ADDF_CO",
        "inputs": ["A", "B", "C"],
        "outputs": {"X": "X"},
    },
    # mux2i: inverting MUX2 — Y = ~(S ? A1 : A0)
    "sky130_fd_sc_hd__mux2i_*": {
        "node_type": "GATE",
        "gate_type": "MUX2I",
        "inputs": ["A0", "A1", "S"],
        "outputs": {"Y": "Y"},
    },
    # mux4: 4-to-1 mux; A0..A3=data, S0=sel0, S1=sel1
    "sky130_fd_sc_hd__mux4_*": {
        "node_type": "GATE",
        "gate_type": "MUX4",
        "inputs": ["A0", "A1", "A2", "A3", "S0", "S1"],
        "outputs": {"X": "X"},
    },
    # --- new compound GateTypes ---
    # a211o: (A1&A2)|B1|C1
    "sky130_fd_sc_hd__a211o_*": {
        "node_type": "GATE",
        "gate_type": "A211O",
        "inputs": ["A1", "A2", "B1", "C1"],
        "outputs": {"X": "X"},
    },
    # a222oi: ~((A1&A2)|(B1&B2)|(C1&C2))
    "sky130_fd_sc_hd__a222oi_*": {
        "node_type": "GATE",
        "gate_type": "A222OI",
        "inputs": ["A1", "A2", "B1", "B2", "C1", "C2"],
        "outputs": {"Y": "Y"},
    },
    # a2bb2oi: (A1_N|A2_N)&~(B1&B2)
    "sky130_fd_sc_hd__a2bb2oi_*": {
        "node_type": "GATE",
        "gate_type": "A2BB2OI",
        "inputs": ["A1_N", "A2_N", "B1", "B2"],
        "outputs": {"Y": "Y"},
    },
    # a311o: (A1&A2&A3)|B1|C1
    "sky130_fd_sc_hd__a311o_*": {
        "node_type": "GATE",
        "gate_type": "A311O",
        "inputs": ["A1", "A2", "A3", "B1", "C1"],
        "outputs": {"X": "X"},
    },
    # a311oi: ~((A1&A2&A3)|B1|C1)
    "sky130_fd_sc_hd__a311oi_*": {
        "node_type": "GATE",
        "gate_type": "A311OI",
        "inputs": ["A1", "A2", "A3", "B1", "C1"],
        "outputs": {"Y": "Y"},
    },
    # a41o: (A1&A2&A3&A4)|B1
    "sky130_fd_sc_hd__a41o_*": {
        "node_type": "GATE",
        "gate_type": "A41O",
        "inputs": ["A1", "A2", "A3", "A4", "B1"],
        "outputs": {"X": "X"},
    },
    # and3b: ~A_N & B & C
    "sky130_fd_sc_hd__and3b_*": {
        "node_type": "GATE",
        "gate_type": "AND3B",
        "inputs": ["A_N", "B", "C"],
        "outputs": {"X": "X"},
    },
    # and4b: ~A_N & B & C & D
    "sky130_fd_sc_hd__and4b_*": {
        "node_type": "GATE",
        "gate_type": "AND4B",
        "inputs": ["A_N", "B", "C", "D"],
        "outputs": {"X": "X"},
    },
    # o211a: (A1|A2)&B1&C1
    "sky130_fd_sc_hd__o211a_*": {
        "node_type": "GATE",
        "gate_type": "O211A",
        "inputs": ["A1", "A2", "B1", "C1"],
        "outputs": {"X": "X"},
    },
    # o21ba: (A1|A2)&~B1_N
    "sky130_fd_sc_hd__o21ba_*": {
        "node_type": "GATE",
        "gate_type": "O21BA",
        "inputs": ["A1", "A2", "B1_N"],
        "outputs": {"X": "X"},
    },
    # o221a: (A1|A2)&(B1|B2)&C1
    "sky130_fd_sc_hd__o221a_*": {
        "node_type": "GATE",
        "gate_type": "O221A",
        "inputs": ["A1", "A2", "B1", "B2", "C1"],
        "outputs": {"X": "X"},
    },
    # o2111a: (A1|A2)&B1&C1&D1
    "sky130_fd_sc_hd__o2111a_*": {
        "node_type": "GATE",
        "gate_type": "O2111A",
        "inputs": ["A1", "A2", "B1", "C1", "D1"],
        "outputs": {"X": "X"},
    },
    # o2111ai: ~((A1|A2)&B1&C1&D1)
    "sky130_fd_sc_hd__o2111ai_*": {
        "node_type": "GATE",
        "gate_type": "O2111AI",
        "inputs": ["A1", "A2", "B1", "C1", "D1"],
        "outputs": {"Y": "Y"},
    },
    # o2bb2ai: (A1_N&A2_N)|~(B1|B2)
    "sky130_fd_sc_hd__o2bb2ai_*": {
        "node_type": "GATE",
        "gate_type": "O2BB2AI",
        "inputs": ["A1_N", "A2_N", "B1", "B2"],
        "outputs": {"Y": "Y"},
    },
    # o41a: (A1|A2|A3|A4)&B1
    "sky130_fd_sc_hd__o41a_*": {
        "node_type": "GATE",
        "gate_type": "O41A",
        "inputs": ["A1", "A2", "A3", "A4", "B1"],
        "outputs": {"X": "X"},
    },
    # o41ai: ~((A1|A2|A3|A4)&B1)
    "sky130_fd_sc_hd__o41ai_*": {
        "node_type": "GATE",
        "gate_type": "O41AI",
        "inputs": ["A1", "A2", "A3", "A4", "B1"],
        "outputs": {"Y": "Y"},
    },
    # --- enable D flip-flop with scan (edfxtp) ---
    # DE = data enable (HIGH); A = scan in; TE_B = scan enable (active LOW)
    # Enable is not modeled (conservative: treat as scan DFF; DE held high by ATPG)
    "sky130_fd_sc_hd__edfxtp_*": {
        "node_type": "FF",
        "inputs": ["CLK", "D", "DE", "A", "TE_B"],
        "outputs": {"Q": "Q"},
        "ff": {
            "clock": "CLK",
            "data": "D",
            "output": "Q",
            "trigger": "POSEDGE",
            "scan": {
                "in": "A",
                "enable": "TE_B",
                "enable_polarity": "LOW",
            },
        },
    },
}

added = []
skipped = []
for k, v in new_entries.items():
    if k in data:
        skipped.append(k)
    else:
        data[k] = v
        added.append(k)

json.dump(data, open(PATH, "w"), indent=2)
print(f"Added {len(added)} entries:")
for k in sorted(added):
    print(f"  + {k}")
if skipped:
    print(f"Skipped (already present) {len(skipped)}:")
    for k in sorted(skipped):
        print(f"  = {k}")
