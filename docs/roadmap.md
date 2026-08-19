# Roadmap

This is faultflow's forward plan: what is implemented today, and the permanent scope
boundaries beyond which no further core-engine work is planned.

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

No further core-simulation-engine features are planned. faultflow's scope is now considered
complete for flip-flop-only synchronous designs with no tristate — see Scope boundaries
below for what's permanently excluded and why. Smaller, non-core items remain open (STIL/WGL
pattern export, techmap-verify) — see `feature.md` for that shorter list.

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

See [External tools](external_tools.md) for the planned autoMBIST and OpenSTA
directions.
