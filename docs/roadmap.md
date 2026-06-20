# Roadmap

This is the public summary of faultflow's forward plan. The authoritative, detailed
version is [`docs/plans/roadmap.md`](https://github.com/ranaumarnadeem/faultflow/blob/main/docs/plans/roadmap.md)
in the source tree.

## Done

The combinational core, the supporting infrastructure, the verification gate, the
sequential and scan flow, and the native SAT ATPG are implemented:

- Bit-parallel combinational simulation, cross-checked against a scalar golden
  reference; binary (two-valued) signals.
- SQLite campaign database, input fingerprinting and resume, the CLI, the Yosys runner,
  and coverage reporting.
- The optional iverilog verification gate.
- Posedge **and** negedge D flip-flops (simulated, not just mapped), asynchronous
  set/reset, scan-chain insertion, and scan SAT ATPG (single clock).
- Native C++ SAT ATPG on CaDiCaL; Sky130 HD support; equivalence/dominance fault
  collapsing; reverse-order test-set compaction.
- An initial transition-fault flow (combinational broadside, launch-on-capture) and
  IEEE 1500 wrapper test modes.
- An initial DFT rule-check command (`rule_check`), whose rule set continues to grow
  (Phase 5 below).

## Planned sequence

The order below is deliberate — each step is sequenced so the one after it can be built
on solid ground.

| # | Phase | Scope | Touches the sim core? |
|---|---|---|---|
| 4 | **Transition faults** | Slow-to-rise / slow-to-fall, two-frame, single clock, launch-on-capture | frames = 2 only |
| 5 | **DFT rule check (DRC)** | Scan/clock structural rules; an advisory or blocking pre-ATPG gate | no |
| 6 | **Multi-clock** | Domain-aware test protocol; stuck-at first, then at-speed transition | minor |
| 7 | **X-state** | Three-valued simulation core (initialization, unknowns, masking) | yes — the most invasive change |
| 8 | **Latches** | A general level-sensitive latch model, built on X-state | yes |
| — | **TBUF / tristate** | Deferred indefinitely; remains a hard error by policy | — |

```{note}
Phases 4 and 5 build on work already in the tree: an initial transition-fault flow and
the `rule_check` command exist today. These phases formalize the two-frame coverage
accounting and expand the structural rule set, respectively.
```

### Why this order

- **Transition faults first** — the one genuinely new fault model on the list, and its
  foundation (negedge/posedge/async/scan simulation and scan ATPG) already exists.
- **DFT rule check early** — it catches the structural hazards that multi-clock and scan
  introduce, so the later coverage numbers are trustworthy. It does not touch the sim
  core, so it is low-risk.
- **Multi-clock before X-state and latches** — domain-aware protocol is independent of
  X, and the current ISCAS targets are single-clock edge-flop designs.
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
