# Concepts

A short, practical glossary of the ideas faultflow works with. None of this is
specific to faultflow's internals — see the
[Architecture overview](../architecture/overview.md) for how they are implemented.

## Stuck-at faults

The **stuck-at fault model** abstracts a manufacturing defect as a single net being
permanently tied to a logic value:

- **SA0** — the net is stuck at logic 0.
- **SA1** — the net is stuck at logic 1.

faultflow enumerates both polarities on every fault site. A test *detects* a fault if
applying an input vector makes an observable output (a primary output, or a tagged
test point) differ between the fault-free circuit and the faulty one.

## Transition faults

A **transition fault** models a defect that is slow rather than stuck: a net that can
eventually reach the right value but not within the clock period.

- **Slow-to-rise (STR)** — a 0→1 transition is too slow.
- **Slow-to-fall (STF)** — a 1→0 transition is too slow.

Detecting one needs a *pair* of vectors: one to set up the starting value, and one to
launch the transition and capture the result. faultflow implements the combinational
broadside (two-pattern) flavor with launch-on-capture (LOC). Transition faults are
enabled with `[fault_model] model = transition`.

## Fault sites and the checkpoint model

Rather than placing a fault on every pin of every gate, faultflow uses the
**checkpoint theorem**: it is sufficient to consider faults on primary inputs and on
every *fanout branch*. The compiler materializes each fanout branch as its own net, so
"a fault on a branch" becomes an ordinary net fault. This keeps the fault list small
without losing coverage fidelity.

## Fault collapsing

Many faults are logically equivalent (always detected together) or dominated by
others. **Collapsing** removes the redundant ones from the fault list so coverage is
computed over a minimal, non-redundant set. faultflow applies verified equivalence and
dominance rules for simple primitives (INV/BUF equivalence, AND/OR/NAND/NOR
dominance) and deliberately leaves compound cells uncollapsed. Collapsing is off by
default (`[fault_model] collapsing = false`).

## ATPG

**Automatic Test Pattern Generation** is the search for input vectors that detect the
faults a random/functional vector set missed. faultflow's ATPG is **native and
SAT-based**: for each target fault it builds a fault-free and a faulty CNF copy of the
circuit, forces the stuck-at value, adds a constraint that some observable output must
differ, and asks the [CaDiCaL](https://github.com/arminbiere/cadical) solver for a
satisfying assignment.

- **SAT** → the assignment is a test vector (re-simulated to confirm before it is
  trusted).
- **UNSAT** → the fault is **redundant** (untestable) under the current model.
- **TIMEOUT / UNKNOWN** → the fault is left undetected, never marked redundant.

The ATPG runs in a **progressive loop**: generate, fault-simulate to detect
"fallout" faults the new vectors happen to cover, repeat until the coverage threshold,
a round limit, or a stall is reached.

## Coverage and the denominator

Coverage is only meaningful relative to a precisely defined denominator. faultflow's
rule:

```text
denominator = faults where ALL of:
  - the net belongs to a supported (non-blackboxed) cell
  - the fault is not collapsed away
  - the net is not a clock   (unless include_clock_faults = true)
  - the net is not a reset   (unless include_reset_faults = true)

coverage = detected / denominator x 100
```

Every fault is still *enumerated and counted*; exclusions (blackbox, clock, reset) are
**tagged and reported separately**, never silently dropped. A zero denominator is an
error, never a divide-by-zero.

The report gives two coverage figures that differ only in how they treat faults the
ATPG *proved untestable* (redundant): **test coverage** removes those from the
denominator (`detected / testable`), while **fault coverage** keeps them in
(`detected / structurally-eligible`), so fault coverage is always the more
conservative number. See [Outputs](../user_guide/outputs.md) for the exact fields.

## Scan

Sequential logic is hard to test directly because flip-flop state is not observable.
**Scan** chains the flip-flops into a shift register (a scan chain) so their state can
be shifted in (controlled) and shifted out (observed). This turns the flip-flops into
pseudo-primary-inputs and pseudo-primary-outputs, reducing sequential ATPG to the
combinational problem faultflow already solves. faultflow can insert generic scan
chains, check them, map them to Sky130 scan cells, and run scan ATPG.

## Test modes (IEEE 1500)

For a core wrapped with an IEEE 1500 boundary, faultflow supports three wrapper test
modes: **functional** (wrapper transparent), **intest** (test the core internals), and
**extest** (test the interconnect around the core). These are selected in the
[Tcl shell](../user_guide/tcl_shell.md) or via the `[testmode]` config section.

## A few synthesis terms

faultflow sits downstream of synthesis, so a little vocabulary leaks in:

- **Liberty (`.lib`)** — the standard-cell library file (timing, function). faultflow
  passes it to Yosys only; the core reads the JSON
  [cell map](../architecture/cell_libraries.md) instead.
- **Techmap** — mapping generic cells onto a specific library's cells. faultflow
  techmaps generic scan cells onto Sky130 scan flip-flops.
- **BENCH** — a simple ISCAS netlist format, used by the optional Quaigh path and by the
  `--ext` sidecar to fix primary-input order.
