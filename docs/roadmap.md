# Roadmap

This is faultflow's forward plan: what is implemented today, what is planned next, and what
stays out of scope.

## Done

Implemented today:

- Bit-parallel combinational simulation, cross-checked against a scalar golden
  reference; binary (two-valued) signals.
- SQLite campaign database, input fingerprinting and resume, the Tcl shell and the
  batch CLI, the Yosys runner, and coverage reporting.
- The optional iverilog verification gate.
- Posedge **and** negedge D flip-flops (simulated, not just mapped), asynchronous
  set/reset, scan-chain insertion, and scan SAT ATPG.
- Native C++ SAT ATPG on CaDiCaL, with cone-of-influence CNF restriction, escalating
  per-fault timeouts, parallel multi-process solving, equivalence-only fault
  collapsing (never dominance, so coverage is provably unchanged), and both
  reverse-order and dynamic (sim-verified cube-packing) test-set compaction.
- Two PDKs: SkyWater Sky130 HD (default) and OSU035, driven by JSON cell maps.
- **Transition faults** — slow-to-rise / slow-to-fall, two-frame combinational broadside
  plus scan launch-on-capture (LOC) and launch-on-shift (LOS), with a two-frame iverilog
  verification gate.
- **IEEE 1500 wrapper test modes** (functional / INTEST / EXTEST), a native shiftable
  wrapper boundary register, hierarchical block-to-SoC coverage aggregation, and
  scan-pattern retargeting.
- **Multi-clock** domain-aware test protocol (per-domain at-speed; cross-domain paths
  masked).
- A **DFT rule check** command (`rule_check`) whose structural rule set continues to grow.
- OpenTestability test-point insertion (`add_tp` / `reject_tp`) and coverage-oracle mode
  (`run -c`).
- **Blackbox instances** (`[blackbox]`): memories and macros modeled as test
  boundaries, or, in scan, as opaque cells whose unknown outputs are masked.
- **Scan test-pattern compression and response compaction** (`scan-compress` /
  `scan-compact`): a continuous-flow decompressor (a ring generator that takes the channel
  bits on every shift, and a phase shifter) in front of the scan inputs and a static
  XOR-tree space compactor behind the scan outputs, each structurally self-checked. SAT
  ATPG solves through the decompressor, so every compressed pattern is a load it can
  make, and the composed chip is written as sky130 Verilog.
- **Scan pattern export**: JSON (`sim --scan --export-patterns`) and STIL (IEEE 1450,
  `write-patterns`), with each load overlapping the previous unload by default. Every
  exported flow is replayed on the sky130 cell models in the test suite.
- **Per-fault CNF export** (`write-cnf`): the CNF native ATPG solves for one fault, as
  DIMACS with a JSON variable map, for other solvers and proof checkers.
- **Scan-cell techmap verification** (`write_netlist -scan -techmap -verify`): the
  techmapped netlist is fault-free simulated against the generic scan design.
- **autoMBIST integration** (`autombist-generate`): autoMBIST's MBIST design
  synthesized for scan test, memories as blackboxes, optionally wrapped for JTAG
  access, with coverage broken down by instance category.
- **MBIST insertion** into a user's chip RTL (`list-memories`, `mbist-insert`): an
  autoMBIST collar, in a shell, in place of each configured memory, optionally behind a
  TAP with a dedicated IJTAG_ACCESS instruction and an IJTAG network (warptap); frozen
  DFT synthesis, settled non-scan reset synchronizers, and RTL and netlist SDCs of the
  inserted crossings.
- **JTAG network-integrity grading** (`jtag`): a TCK program played through the TAP of
  the scanned netlist and graded at TDO, after an X-isolation proof, with scan and JTAG
  coverage combined.
- **A cell-level IEEE 1500 wrapper** (`wrap`): boundary cells of real sky130 cells, set by
  mode pins or an IEEE 1500 wrapper serial port (WIR, WBY, test setup programs); INTEST,
  EXTEST, the SoC project flow and retargeting as scan tests that replay on the cells;
  the WSP's own faults graded by its integrity program (`wsp`).

## Planned

- **Unknown values: X, Z and latches.** A second simulation engine that carries 0, 1, X and
  Z, chosen per run. The two-valued engine stays as it is, so every result it gives today is
  unchanged. Every cell input reads Z as X, as Verilog gate primitives do. In order:
  1. A scalar reference simulator with X, every gate type checked against the existing
     truth tables.
  2. The bit-parallel engine with X (two words per net), checked lane by lane against the
     reference. With no X in a run, it must give exactly the two-valued engine's results.
  3. Unknown power-up state of flops, and a fault's detections split into definite (a 0
     against a 1 at an output) and possible (a known value against X). Only definite
     detections count toward coverage.
  4. Level-sensitive latches: transparent while enabled, holding otherwise, starting at X.
  5. Combinational loops, each settled to a fixed point starting from X; a loop that does
     not settle reads X.
  6. Tristate buffers and buses driving Z: one enabled driver gives its value, none gives
     Z, a conflict gives X.

  SAT test generation stays two-valued: an unknown source is masked, and every pattern is
  graded by the simulator, which decides definite against possible.
- **Visualization** (`ff.py viz`): scan chains, the cone around a fault, and where
  undetected faults cluster, as one self-contained HTML file built from a run's outputs,
  with no change to the simulator. Static views come first: scan chains coloured by
  coverage, one fault's cone, and coverage by instance. An interactive viewer follows only
  if those prove useful.

## Scope boundaries

Until the engine above lands, the simulator is two-valued: a latch or tristate cell is a
hard error, and an `x` or `z` bit in a netlist is tied to 0. These were once listed as
permanent exclusions. That decision was reversed on 2026-11-10: latches need an unknown
state, and the earlier reason (that Verilator took years to get X-state right) does not hold,
since Verilator is a two-state simulator by design.

These stay out of scope:

- **Clock-domain-crossing (CDC) verification is out of scope.** Metastability and
  synchronizer checking are a separate signoff concern, not a fault-testing one. Multi-
  clock *fault testing* (modeling more than one clock so flip-flops clock correctly
  during the test protocol) is in scope; CDC *verification* is not.
- **OpenSTA is an optional, later timing source only** — real path delays for the
  at-speed capture window — never a CDC provider. Transition testing ships on metadata
  timing first.

See [External tools](external_tools.md) for the autoMBIST integration and the planned
OpenSTA direction.
