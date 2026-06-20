# About faultflow

faultflow is a standalone, gate-level **fault simulator** and **automatic test
pattern generator (ATPG)**. It takes a netlist that has already been synthesized to
standard cells, enumerates manufacturing-defect faults on it, and answers two
questions:

1. **How many of those faults can a given set of test vectors detect?** (fault
   simulation / coverage grading)
2. **What test vectors detect the remaining faults?** (ATPG)

It is aimed at post-synthesis netlists from [Yosys](https://yosyshq.net/yosys/),
mapped to a supported standard-cell library (Sky130 HD or OSU035).

## Design in one picture

```text
   Verilog RTL
        |
        v
   Yosys synthesis  (read_verilog -> synth -> dfflibmap -> abc -> write_json)
        |
        v
   Yosys JSON netlist  ----------------------------+
        |                                          |
        v                                          v
   faultflow core (C++17)                    Gate-level Verilog
   ParsedGraph -> NormalizedGraph                  |
        -> CompiledSimGraph                        v
        -> fault enumeration                  iverilog verification
        -> SAT ATPG (CaDiCaL)                 (optional gate)
        -> bit-parallel fault simulation
        |
        v
   coverage.rpt + patterns.test + SQLite campaign DB
```

The Python layer orchestrates the flow (synthesis, ATPG loop, reporting); the C++
core does the heavy lifting (IR compilation, simulation, SAT solving). See the
[Architecture overview](architecture/overview.md) for the details.

## Feature matrix

The following capabilities are **implemented and working today**:

| Area | Capability |
|---|---|
| Fault models | Stuck-at (SA0/SA1); transition (slow-to-rise / slow-to-fall, broadside two-pattern) |
| Simulation | 64-lane bit-parallel engine; scalar golden-reference cross-check; binary (two-valued) signals |
| ATPG | Native SAT (CaDiCaL); progressive loop; simulator-verified vectors; redundancy (UNSAT) classification |
| Fault handling | Checkpoint enumeration; equivalence/dominance collapsing; reverse-order test-set compaction |
| Sequential | Posedge and negedge D flip-flops; asynchronous set/reset (clear/preset with polarity) |
| Scan | Generic scan-chain insertion and stitching; scan checking; scan SAT ATPG; Sky130 scan-cell techmap |
| Test modes | IEEE 1500 wrapper modes (functional / intest / extest) |
| PDKs | Sky130 HD (default) and OSU035, via JSON cell maps |
| Verification | Optional iverilog gate that re-simulates the gate-level netlist against golden outputs |
| Interfaces | `ff.py` argparse CLI, an interactive Tcl shell, and an OpenTestability "oracle" mode |

The following are **planned** and described in the [Roadmap](roadmap.md): expanded
multi-clock support, three-valued (X-state) simulation, a general latch model, and
tristate/TBUF handling (currently a hard error by policy). The `rule_check` DFT
rule-check command exists today as an initial thread whose rule set continues to grow.

## What faultflow is not

- It is **not** a logic synthesis tool. Synthesis is delegated to Yosys.
- It is **not** a static timing or clock-domain-crossing (CDC) signoff tool. CDC is
  explicitly out of scope; at-speed timing from a tool such as OpenSTA is a possible
  *future* input, not a current feature.
- It does **not** model memory BIST. autoMBIST is listed under
  [external tools](external_tools.md) as a future direction only.

## License

faultflow is released under the Apache-2.0 license.
