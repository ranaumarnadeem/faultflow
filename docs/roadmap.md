# Roadmap

This is faultflow's forward plan: what is implemented today, and the permanent scope
boundaries beyond which no further core-engine work is planned.

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
  `scan-compact`): a ring-generator and phase-shifter decompressor in front of the scan
  inputs and a static XOR-tree space compactor behind the scan outputs, each
  structurally self-checked. SAT ATPG solves through the decompressor, so every
  compressed pattern is a load it can make, and the composed chip is written as sky130
  Verilog.
- **Scan pattern export**: JSON (`sim --scan --export-patterns`) and STIL (IEEE 1450,
  `write-patterns`), with each load overlapping the previous unload by default. Every
  exported flow is replayed on the sky130 cell models in the test suite.
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

No further core-simulation-engine features are planned. faultflow's scope is now considered
complete for flip-flop-only synchronous designs with no tristate — see Scope boundaries
below for what's permanently excluded and why. DFT items still open:

- A continuous-flow decompressor (channel data injected on every shift cycle) in place
  of today's one seed per pattern.
- Scan insertion of flip-flops whose D input synthesis tied to a constant.
- Cells missing from the cell map treated as unknown in every flow when
  `unsupported_cells = blackbox`.
- Scan clocks traced to their ports through buffers and held gates.
- Incremental SAT and lower memory use for transition faults.

## Scope boundaries

The following are permanent, deliberate exclusions — decided against, not deferred pending
future capacity. Revisiting any of these would be a scope change, not a backlog item.

- **X-state (three-valued simulation) — decided against.** Would have been the most
  invasive possible change: the gate-evaluation table, the bit-parallel word layout, and the
  fault records were all built two-valued on purpose. The deciding factor: even mature tools
  with far larger contributor bases (Verilator, for one) took years to get X-state right
  despite heavy investment — the cost/risk wasn't judged worth it for this project's scope.
- **General level-sensitive latch model — excluded as a consequence of the X-state
  decision.** A correct latch model needs X-state for its uninitialized hold state and
  transparent-phase behavior, so excluding X-state excludes latches with it, not as a
  separate call.
- **TBUF / tristate — deferred indefinitely; remains a hard error by policy.** High-Z is
  itself an X-like third value, so this was already downstream of the X-state decision.
- **Clock-domain-crossing (CDC) verification is out of scope.** Metastability and
  synchronizer checking are a separate signoff concern, not a fault-testing one. Multi-
  clock *fault testing* (modeling more than one clock so flip-flops clock correctly
  during the test protocol) is in scope; CDC *verification* is not.
- **OpenSTA is an optional, later timing source only** — real path delays for the
  at-speed capture window — never a CDC provider. Transition testing ships on metadata
  timing first.

**Net effect**: faultflow's permanent scope is flip-flop-only synchronous designs, no
tristate. This is the tool's intended, final scope for its core simulation/ATPG engine, not
an interim state.

See [External tools](external_tools.md) for the autoMBIST integration and the planned
OpenSTA direction.
