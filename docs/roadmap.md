# Roadmap

This is faultflow's forward plan: what is implemented today, and what is planned next.

## Done

The combinational core, the supporting infrastructure, the verification gate, the
sequential and scan flow, the native SAT ATPG, transition faults, multi-clock, and the
DFT rule check are all implemented:

- Bit-parallel combinational simulation, cross-checked against a scalar golden
  reference; binary (two-valued) signals.
- SQLite campaign database, input fingerprinting and resume, the CLI, the Yosys runner,
  and coverage reporting.
- The optional iverilog verification gate.
- Posedge **and** negedge D flip-flops (simulated, not just mapped), asynchronous
  set/reset, scan-chain insertion, and scan SAT ATPG (single clock).
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
- OpenTestability test-point insertion (`add_tp` / `reject_tp`).

## Planned

Two pieces of work remain, sequenced deliberately so the second builds on the first.

| Work | Scope | Touches the sim core? |
|---|---|---|
| **X-state** | Three-valued simulation core (initialization, unknowns, masking) | yes — the most invasive change |
| **Latches** | A general level-sensitive latch model, built on X-state | yes |
| **TBUF / tristate** | Deferred indefinitely; remains a hard error by policy | — |

### Why this order

- **X-state before latches — a hard ordering.** A general latch model needs X for its
  uninitialized hold state and transparent-phase behavior, so X is done first and the
  latch model is written once, completely. X-state is also the most invasive change,
  because the gate-evaluation table, the bit-parallel word layout, and the fault records
  were all built two-valued on purpose.
- **TBUF/tristate skipped** — high-Z is itself an X-like third value; deferring it keeps
  the X-state scope smaller. It stays hard-failing, consistent with the unsupported-cell
  policy.

## Scope boundaries

- **Clock-domain-crossing (CDC) verification is out of scope.** Metastability and
  synchronizer checking are a separate signoff concern, not a fault-testing one. Multi-
  clock *fault testing* (modeling more than one clock so flip-flops clock correctly
  during the test protocol) is in scope; CDC *verification* is not.
- **OpenSTA is an optional, later timing source only** — real path delays for the
  at-speed capture window — never a CDC provider. Transition testing ships on metadata
  timing first.

See [External tools](external_tools.md) for the planned autoMBIST and OpenSTA
directions.
