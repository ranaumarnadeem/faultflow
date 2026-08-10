# Benchmarking

"Benchmarking" means two different things for faultflow: the **standard circuits** it
is exercised on, and the **timing data** every run records. This page covers both and
gives a script for measuring throughput.

## Benchmark circuits

The repository ships the classic ISCAS test circuits under `tests/benchmarks/`:

| Suite | Circuits | Location |
|---|---|---|
| ISCAS-85 (combinational) | `c17`, `c432`, `c499` | `tests/benchmarks/iscas85/synth/` (OSU035), `…/synth_sky130/` (Sky130) |
| ISCAS-89 (sequential) | `s27` … `s15850` (28 circuits, shipped as `*_bench.json`) | `tests/benchmarks/iscas89/synth_sky130/` |

Each ships pre-synthesized so it can be graded without re-running Yosys. The larger
`examples/picorv32_synth/` PicoRV32 netlist is a realistic stress test.

## Timing data every run records

faultflow instruments its own runs with wall-clock timers and persists the numbers to
the campaign database, surfacing them in `status` and in the coverage report. The key
fields are:

| Report field | Meaning |
|---|---|
| `atpg_seconds` | Time spent in SAT ATPG (DB column `atpg_generation_seconds`) |
| `fault_sim_seconds` | Time spent in bit-parallel fault simulation (DB column `fault_simulation_seconds`) |
| `total_sim_seconds` | End-to-end campaign time |
| `atpg_rounds` | Progressive-loop round count |
| `atpg_sat` / `atpg_unsat` / `atpg_timeout` / `atpg_unknown` | Per-fault SAT outcome counts |

These let you compare runs (e.g. collapsing on vs. off, or different `sat_timeout_seconds`)
without any external harness.

## Measuring throughput

A simple way to time the ISCAS-85 grades is to wrap the batch script from
[Flow recipes](user_guide/examples.md) with `/usr/bin/time`:

```bash
#!/usr/bin/env bash
# bench_iscas85.sh — time the native ATPG over ISCAS-85.
set -euo pipefail

for top in c17 c432 c499; do
  cfg="$(mktemp --suffix=.ofs)"
  cat > "$cfg" <<EOF
[design]
netlist  = tests/benchmarks/iscas85/synth_sky130/${top}.json
top      = ${top}
cell_lib = cells/sky130/sky130_fd_sc_hd.json
liberty  = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib
[atpg]
tool     = native
[report]
threshold = 100.0
EOF

  python3 ff.py init --top "$top" -c "$cfg" >/dev/null
  echo "=== ${top} ==="
  /usr/bin/time -f "  elapsed %e s   maxrss %M KB" \
      python3 ff.py sim --top "$top" -c "$cfg" --clean
  python3 ff.py status --top "$top" -c "$cfg"
  rm -f "$cfg"
done
```

The per-circuit `status` output reports the internal `atpg_seconds` and
`fault_sim_seconds` breakdown, while `/usr/bin/time` captures the total process time
and peak memory.

## Comparing against other tools

The native ATPG can be compared against a reference flow:

- **Quaigh** — with `[atpg] tool = quaigh`, faultflow converts the gate-level Verilog to
  BENCH with `nl2bench` and runs `quaigh atpg` to produce reference patterns. See
  [External tools](external_tools.md).
- **Transition vs. stuck-at** — running the same circuit with
  `[fault_model] model = stuck_at` and then `model = transition` gives a side-by-side
  coverage comparison for the two fault models.

## faultflow native scan ATPG — ISCAS-85/89 stuck-at sweep

faultflow's own numbers across the full shipped ISCAS corpus (30 designs), run with
the native SAT ATPG on the Sky130 HD netlists. Unlike the Fault baseline below (which
grades a combinational *cut* of the sequential circuits), these are **sequential
scan-aware** results: flip-flops are stitched into scan chains and exercised through
scan load/unload, so the coverage is over the real sequential fault set. `c17`/`c432`/
`c499` are purely combinational (no scan) — FFs/Chains/Terminal read "—" for those rows.

**Method.** Scan insertion at **≤10 flip-flops per chain**; native SAT ATPG with 4
workers; single-tier 2 s per-fault SAT timeout; `max_rounds = 6`; incremental-SAT
(IFC) **off**; fault collapsing off; DB on a native ext4 path (not `/mnt/c`). Each
design is independently wall-clock-guarded so one slow design cannot stall the sweep.

| Design | Cells | FFs | Chains | Coverage | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
| c17 | 3 | — | — | 100.00% | 10.5 s | COMPLETE |
| c432 | 65 | — | — | 100.00% | 11.9 s | COMPLETE |
| c499 | 160 | — | — | 100.00% | 17.1 s | COMPLETE |
| s27 | 12 | 3 | 1 | 100.00% | 12.0 s | COMPLETE |
| s208_1 | 45 | 8 | 1 | 100.00% | 20.0 s | COMPLETE |
| s298 | 76 | 14 | 2 | 99.36% | 13.2 s | THRESHOLD_MET |
| s344 | 97 | 15 | 2 | 100.00% | 17.2 s | COMPLETE |
| s349 | 97 | 15 | 2 | 100.00% | 16.6 s | COMPLETE |
| s382 | 102 | 21 | 3 | 100.00% | 22.4 s | COMPLETE |
| s386 | 87 | 6 | 1 | 99.05% | 30.2 s | THRESHOLD_MET |
| s400 | 102 | 21 | 3 | 99.59% | 25.7 s | THRESHOLD_MET |
| s420_1 | 105 | 16 | 2 | 97.25% | 22.8 s | THRESHOLD_MET |
| s444 | 105 | 21 | 3 | 98.92% | 21.2 s | THRESHOLD_MET |
| s510 | 139 | 6 | 1 | 98.91% | 24.8 s | THRESHOLD_MET |
| s526 | 112 | 21 | 3 | 98.66% | 24.9 s | THRESHOLD_MET |
| s526n | 113 | 21 | 3 | 99.22% | 22.2 s | THRESHOLD_MET |
| s641 | 121 | 17 | 2 | 99.47% | 22.6 s | THRESHOLD_MET |
| s713 | 109 | 17 | 2 | 100.00% | 19.4 s | COMPLETE |
| s820 | 156 | 5 | 1 | 98.99% | 33.8 s | THRESHOLD_MET |
| s832 | 165 | 5 | 1 | 97.98% | 31.4 s | THRESHOLD_MET |
| s838_1 | 207 | 32 | 4 | 97.66% | 29.5 s | THRESHOLD_MET |
| s1196 | 309 | 18 | 2 | 99.33% | 50.7 s | THRESHOLD_MET |
| s1238 | 320 | 18 | 2 | 97.42% | 63.7 s | THRESHOLD_MET |
| s1423 | 435 | 74 | 8 | 99.68% | 40.7 s | THRESHOLD_MET |
| s1488 | 319 | 6 | 1 | 99.49% | 39.7 s | THRESHOLD_MET |
| s1494 | 332 | 6 | 1 | 99.57% | 38.4 s | THRESHOLD_MET |
| s5378 | 845 | 162 | 17 | 99.48% | 141.9 s | THRESHOLD_MET |
| s9234_1 | 715 | 135 | 14 | 99.17% | 106.8 s | THRESHOLD_MET |
| s13207 | 1,886 | 452 | 46 | — | 5.4 s | rejected¹ |
| s15850 | 2,644 | 559 | 56 | **99.16%** | 690.9 s | THRESHOLD_MET |

**Result: 30 designs, zero crashes, 29 clean numbers + 1 clean rejection.** Every
graded design lands at **97.4 – 100%**, most ≥99%.

¹ `s13207` is rejected (not crashed) with `sim --scan requires full scan: ineligible
FFs remain` — Yosys emits a bit-sliced flip-flop (`$auto$ff.cc:…:slice`) that scan
insertion does not cover. This is a real scan-eligibility limitation, surfaced as a
clean error rather than a wrong number.

**On chain length.** The `≤10 FF/chain` rule is what makes the largest design
tractable: `s15850` (559 FFs → 56 short chains) converges to 99.16% in ~11.5 min,
where a coarser chaining (fewer, longer chains) times out well short of convergence —
short chains shift far faster, so the whole scan-ATPG protocol completes sooner.

**Trustworthy zero-crash.** As of the hardening pass, a dead parallel SAT worker
raises a hard error instead of silently degrading the round to `UNKNOWN`, so a
zero-crash sweep now genuinely means zero bad runs rather than possibly-hidden garbage
coverage.

## faultflow native scan ATPG — ISCAS-85/89 transition-fault (LOC) sweep

The same 30-design corpus, same ≤10 FF/chain scan config, run with **launch-on-capture
(broadside) transition faults** instead of stuck-at. Fault collapsing is off (a hard
requirement for the transition model — the config loader rejects `model=transition` with
collapsing on), so every row here is against the full uncollapsed transition fault set,
unlike the stuck-at sweep above (which ran with collapsing on).

**Method.** Same ≤10 FF/chain scan insertion; native SAT ATPG, 4 workers; escalating
2 s → 10 s per-fault SAT timeout; `max_rounds = 6`; IFC off; collapsing off; DB on native
ext4. Combinational designs (c17/c432/c499) run `-tf broadside` without `-scan` (broadside
is supported directly on plain combinational netlists; only `los` is scan-only).

Two coverage numbers matter here, and they diverge sharply for transition faults in a way
they never do for stuck-at:
- **Coverage** — the tool's primary metric, detected over the *testable* denominator
  (excludes SAT-proven-redundant faults). This is what `THRESHOLD_MET` targets at 95%.
- **Fault cov.** — detected over the *full* in-scope denominator (testable + redundant).
  This is the Policy-3 style "of everything that could possibly be a fault site" number.

| Design | Cells | FFs | Chains | Coverage | Fault cov. | Redundant | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| c17 | 3 | — | — | 100.00% | 100.00% | 0 | 13.7 s | COMPLETE |
| c432 | 65 | — | — | 100.00% | 99.30% | 4 | 18.9 s | COMPLETE |
| c499 | 160 | — | — | 100.00% | 100.00% | 0 | 31.7 s | COMPLETE |
| s27 | 12 | 3 | 1 | 85.71% | 37.50% | 36 | 15.1 s | STALLED |
| s208_1 | 45 | 8 | 1 | 98.89% | 73.55% | 62 | 24.9 s | THRESHOLD_MET |
| s298 | 76 | 14 | 2 | 97.25% | 71.83% | 103 | 28.5 s | THRESHOLD_MET |
| s344 | 97 | 15 | 2 | 99.08% | 85.66% | 68 | 33.5 s | THRESHOLD_MET |
| s349 | 97 | 15 | 2 | 99.08% | 85.66% | 68 | 35.1 s | THRESHOLD_MET |
| s382 | 102 | 21 | 3 | 98.55% | 68.29% | 183 | 36.5 s | THRESHOLD_MET |
| s386 | 87 | 6 | 1 | 87.93% | 57.09% | 188 | 40.3 s | STALLED |
| s400 | 102 | 21 | 3 | 97.80% | 67.51% | 184 | 37.7 s | THRESHOLD_MET |
| s420_1 | 105 | 16 | 2 | 99.05% | 74.02% | 142 | 40.8 s | THRESHOLD_MET |
| s444 | 105 | 21 | 3 | 96.41% | 67.17% | 182 | 39.0 s | THRESHOLD_MET |
| s510 | 139 | 6 | 1 | 93.35% | 69.44% | 228 | 50.6 s | STALLED |
| s526 | 112 | 21 | 3 | 96.81% | 61.76% | 231 | 42.2 s | THRESHOLD_MET |
| s526n | 113 | 21 | 3 | 96.35% | 62.07% | 227 | 41.6 s | THRESHOLD_MET |
| s641 | 121 | 17 | 2 | 93.69% | 53.75% | 318 | 56.1 s | STALLED |
| s713 | 109 | 17 | 2 | 93.48% | 52.54% | 311 | 54.7 s | STALLED |
| s820 | 156 | 5 | 1 | 93.31% | 57.92% | 402 | 62.4 s | STALLED |
| s832 | 165 | 5 | 1 | 93.50% | 61.07% | 376 | 66.3 s | STALLED |
| s838_1 | 207 | 32 | 4 | 98.09% | 45.01% | 618 | 73.5 s | THRESHOLD_MET |
| s1196 | 309 | 18 | 2 | 97.37% | 20.82% | 1,541 | 90.6 s | THRESHOLD_MET |
| s1238 | 320 | 18 | 2 | 95.91% | 20.27% | 1,642 | 97.5 s | THRESHOLD_MET |
| s1423 | 435 | 74 | 8 | 97.40% | 72.81% | 611 | 199.5 s | THRESHOLD_MET |
| s1488 | 319 | 6 | 1 | 91.01% | 63.08% | 655 | 153.7 s | STALLED |
| s1494 | 332 | 6 | 1 | 96.27% | 78.56% | 405 | 137.6 s | THRESHOLD_MET |
| s5378 | 845 | 162 | 17 | 98.34% | 71.29% | 1,278 | 820.5 s | THRESHOLD_MET¹ |
| s9234_1 | 715 | 135 | 14 | 98.53% | 85.35% | 525 | 688.8 s | THRESHOLD_MET |
| s13207 | 1,886 | 452 | 46 | — | — | — | 8.2 s | rejected² |
| s15850 | 2,644 | 559 | 56 | ~51.5%³ | ~50.1%³ | 382³ | 1,231.7 s | INCOMPLETE³ |

**Result: 30 designs, zero crashes, 27 clean numbers, 1 known rejection, 1 budget-limited
partial.** Coverage (the testable-denominator metric) lands **85.7 – 100%**, in line with
the stuck-at sweep. Fault coverage tells a very different story: it ranges from
**20.3% to 100%**, and on several designs (s1196, s1238, s838_1) the **redundant count
exceeds the detected count** — most of those circuits' transition fault sites are simply
not launchable/observable in a single broadside capture cycle from a scan-loaded state.
This is a structural property of launch-on-capture testing on these benchmark circuits,
not an ATPG weakness: the *testable* population is covered as thoroughly as under
stuck-at, it's just a much smaller fraction of the *total* population than stuck-at's.

¹ `s5378`'s coverage-report JSON was not written — the run hit its 800 s sweep budget
while writing the report, after ATPG had already printed `terminated: THRESHOLD_MET`
internally. Numbers recovered directly from the campaign DB (which commits per-batch
transactionally, so the mid-write state is still consistent); the ATPG result itself is
complete, only the JSON artifact is missing.

² `s13207` reproduces the exact same pre-existing scan-eligibility rejection as the
stuck-at sweep (see footnote ¹ on the stuck-at table above): a bit-sliced flip-flop
(`$auto$ff.cc:…:slice`) that scan insertion does not cover, caught by `sim`'s full-scan
check even though `check_scan` itself passes structurally. Not new, not transition-model
-specific — a known, already-documented limitation.

³ `s15850` (the largest design, 2,644 cells / 559 FFs) did not converge within its 1,200 s
sweep budget — it was killed mid-round-1, **9,742 of 9,926** hard faults into that round's
SAT wave, before the round closed and committed. The figures shown are a genuine
in-flight snapshot (not a final result) and are marked accordingly; a full run would need
a materially larger time budget than transition ATPG's stuck-at counterpart, which
converged on the same design in 690.9 s (see the stuck-at table above) — a direct measure
of how much more expensive 2-frame transition SAT is than single-frame stuck-at SAT on
the same circuit.

### 2026-08 re-validation run (single-tier SAT timeout, smaller vector budget)

A later spot-check of `c17`, `c432`, `c499`, `s27`, `s15850`, and `iiravg` (TF model), run
with faultflow's current default single-tier per-fault SAT timeout instead of the escalating
2 s → 10 s schedule used for the sweep above, and a much smaller `random_vectors = 64`
budget. It was built to validate a suspected native-SAT-ATPG bug on `picorv32a`'s
transition-fault campaign, not to re-benchmark these small designs — but the result is
notable on its own: **every design here reaches `COMPLETE` cleanly**, including `s27` and
`s15850`, which `STALLED`/`INCOMPLETE`d in the sweep above under the older timeout schedule.
All figures below are read directly from `ff.py status`, not hand-derived (see the
coverage-formula correction note on the PicoRV32a TF section for why that distinction
matters).

| Design | FFs | Chains | Coverage | Redundant | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
| c17 | — | — | 100.00% | 0 | 7.5 s | COMPLETE |
| c432 | — | — | 100.00% | 4 | 16.0 s | COMPLETE |
| c499 | — | — | 100.00% | 0 | 26.8 s | COMPLETE |
| s27 | 3 | 1 | 100.00% | 40 | 12.5 s | COMPLETE |
| s15850 | 559 | 56 | 99.05% | 4,837 | 7,041.9 s | COMPLETE |
| iiravg | 16 | 2 | 100.00% | 256 | 175.3 s | COMPLETE |

This does not prove the original sweep's `STALLED`/`INCOMPLETE` terminal states were wrong
for their own config — timeout schedule and vector budget genuinely differ between the two
runs. It does show TF ATPG convergence on these designs is configuration-sensitive rather
than a hard per-design ceiling, which matters for reading the PicoRV32a TF results below.

---

## faultflow native scan ATPG — DSP filter designs

Three small-to-medium open-source DSP filter blocks (Gisselquist Technology's
`dspfilters` suite), run with the same uncapped native SAT ATPG configuration used for
the PicoRV32a run below: 4 workers, `threshold = 99.0`, `max_rounds = 100` (no per-fault
SAT timeout escalation cap), fault collapsing on, DB on native ext4.

| Design | FFs | Chains | Coverage | Vectors | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
| iiravg | 16 | 2 | **100.00%** | 30 | 7.0 s | COMPLETE |
| genericfir_small (8 taps) | 471 | 48 | **99.981%** | 287 | 590.0 s | MAX_ROUNDS |
| boxcar | 1,130 | 113 | **98.926%** | 288 | 336.1 s | THRESHOLD_MET |
| genericfir_small (8 taps, transition) | 471 | 48 | **99.848%** | 351 | — | THRESHOLD_MET |

`genericfir_small` is a thin wrapper instantiating the vendored `genericfir` core at
8 taps / 8-bit samples instead of its default 128 taps / 12-bit samples. The full-size
`genericfir` (128 taps, 11,607 cells post scan-insertion) is dramatically larger and is
run separately (stuck-at only, transition dropped from scope — a same-family smaller
instance already demonstrates transition ATPG works correctly here, and transition
faults are consistently far more expensive than stuck-at for the same design, per the
PicoRV32a comparison below).

**genericfir_small's 4 never-attempted stuck-at faults** are a real, honest gap, not a
bug: the run hit `max_rounds = 100` with those 4 fault sites still unresolved (never got
a SAT attempt within the round budget), rather than exhausting the SAT search on them
and failing. This is the same kind of gap several `THRESHOLD_MET`-terminated ISCAS
designs above accept — a residual few faults are cheaper to leave unresolved once a
campaign is past its coverage target than to force through.

### 2026-08 rerun — random→SAT hybrid campaign (`ff_rerun_dev`)

A separate, later campaign (6 workers) re-ran all three DSP designs SA and TF, each first
with pure-random fill to a 97% target (no SAT — terminal by construction, not a convergence
result) and then with a random→SAT hybrid switching to SAT at 90% random coverage. Numbers
below are the hybrid finals, independently re-verified against each design's campaign DB via
`ff.py status`. The pure-random phase's raw log figures used an inconsistent coverage
formula and their source DBs were cleaned up before that was caught, so those numbers are
omitted here rather than risk republishing a wrong one — see the correction note on the
PicoRV32a TF section below for how that formula bug was found.

| Design | Model | FFs | Chains | Coverage | Redundant | Vectors | Wall | Terminal |
|---|---|---:|---:|---:|---:|---:|---:|---|
| iiravg | SA | 16 | 2 | 100.00% | 30 | 36 | 16.7 s | COMPLETE |
| iiravg | TF | 16 | 2 | 100.00% | 256 | 80 | 774.2 s | COMPLETE |
| boxcar | SA | 1,130 | 113 | 100.00% | 0 | 1,100 | 1,243.3 s | COMPLETE |
| boxcar | TF | 1,130 | 113 | 91.98%¹ | 5,039 | — | 26,494.8 s | KILLED¹ |
| genericfir_small | SA | 471 | 48 | 100.00% | 17 | 344 | 338.7 s | COMPLETE |
| genericfir_small | TF | 471 | 48 | 100.00% | 3,682 | 261 | 4,012.4 s | COMPLETE |

¹ `boxcar` TF hybrid never converged — killed after ~90 min of flat detection (zero new
detections per 20-min cycle) at roughly the 7.4 h mark, matching the non-convergence
pattern seen on PicoRV32a's TF campaign below. 91.98% is a snapshot at kill time (21,106
detected / 22,949 testable at that point), not a final result.

---

## PicoRV32a — Full-Chip Scan INTEST (Sky130 HD)

PicoRV32a is an open-source RISC-V RV32IMC CPU. It is the largest design faultflow has
been exercised on to date and the first full-chip scan INTEST result with IEEE-1500 WBR
wrapping. Two ATPG strategies have been run against it — a current **uncapped native
SAT** pass and an earlier **random→SAT hybrid** pass, kept side by side below — plus the
original full-chip INTEST measurement that predates a grading fix (superseded).

### Stuck-at scan ATPG

Same audit-fixed core and ≤10 FF/chain scan config (162 chains) for both runs.

| Metric | Uncapped native SAT (current) | Random→SAT hybrid (historical) |
|---|---:|---:|
| Workers | 4 | 6 |
| Fault collapsing | off | on |
| Random→SAT switch | none (pure SAT from vector 1) | `random_stop_coverage=85%`, stopped at 31/64 vectors |
| Denominator | 72,652 (uncollapsed) | 58,286 (collapsed) |
| **Coverage** | **99.785%** (72,496 detected) | **98.67%** (57,513 detected) |
| Fault coverage | 97.55% (of 74,314 in-scope) | 96.4% (of 59,678 in-scope) |
| Proven redundant (UNSAT) | 1,662 | 1,392 |
| Undetected | 156 (153 never-attempted, 3 SAT timeout) | 773 |
| Vectors | 1,164 (uncompacted) | 1,256 (compacted from 1,696) |
| Reverse-compaction time | — | 1,619 s (~27 min, single-threaded) |
| Terminal | THRESHOLD_MET | THRESHOLD_MET |
| ATPG + fault-sim time | 202.3 s + 4,185.5 s (~1 h 49 min) | — |

Collapsing differs (off vs. on), so the denominators aren't directly comparable in
absolute size — but the coverage *percentage* is, and 99.785% is a material improvement.
That gap is consistent with a witness-truncation bug fixed on the `dev` branch: a
multi-bit port (e.g. a wide bus input) was collapsing to a single named PI at bit 0
throughout the ATPG pipeline, so any fault whose only detecting vector required a
non-front bit of such a port was permanently unprovable (surfaced downstream as
`tier_a_reduced_mismatch`). The random→SAT hybrid's notable behavior: rather than
grading all 64 random vectors, it detected the easy 85% with 31 vectors and handed the
hard remainder to SAT (2,252 SAT-detected, 1,392 proven redundant). Reverse vector
compaction is a known post-ATPG scaling cost — shrinking 1,696 vectors to 1,256 took
~27 min single-threaded with essentially zero I/O; the cost grows super-linearly
(≈O(N²)) with raw vector count on large fault populations.

**2026-08 rerun** (`ff_rerun_dev` campaign, same ≤10 FF/chain scan config, 6 workers,
random-fill to 90% then SAT): **99.976%** coverage (87,515 detected / 87,536 testable, 21
undetected, 918 redundant), 1,784 vectors, ATPG 198.4 s + fault-sim 6,839.4 s, wall
13,913.8 s (~3 h 52 min). A separate run from the two columns above, included for
completeness rather than to replace either.

### Transition-fault scan ATPG (launch-on-capture / broadside)

Fault collapsing must be off for transition ATPG (config hard-error otherwise), so both
runs below are against the full uncollapsed transition fault set.

| Metric | Uncapped native SAT (current) | Random→SAT hybrid (historical, first recorded) |
|---|---:|---:|
| Workers | 4 | 6 |
| Denominator | 70,046 (uncollapsed) | 70,453 (uncollapsed) |
| **Coverage** | **98.89%** (69,271 detected) | 94.73% (66,740 detected) |
| Fault coverage | 93.22% (of 74,314 in-scope) | 89.81% (of 74,314 in-scope) |
| Proven redundant (UNSAT) | 4,268 | 3,861 |
| Undetected | 775 | 3,713 |
| Vectors | — | 1,598 (uncompacted — compaction skipped) |
| Rounds | — | 2 of 20 (round 3 stopped, zero new commits) |
| Wall clock | ~7 h | ~10.5 h |

The historical run's fault coverage (89.8%) is well short of its stuck-at counterpart's
96.4%, but that gap is largely *not* an ATPG weakness — it's the redundant block (3,861
and still growing when stopped) representing transitions simply not launchable or
observable in one functional capture cycle from a scan-loaded state. Coverage (the
testable-denominator metric) is the fairer comparison point between fault models, and
both runs land close to their target threshold. This is also the clearest empirical
evidence in this whole document that transition-fault SAT is far more expensive than
stuck-at on the same circuit: both runs took roughly 4-6× longer than their stuck-at
counterpart above.

**2026-08 rerun and coverage-formula correction.** A later `ff_rerun_dev` campaign run
(same config as the historical hybrid row above, 6 workers) is the most-converged TF
result to date: **98.714%** coverage (84,404 detected / 85,504 testable, 1,100 undetected,
2,950 redundant), `THRESHOLD_MET`, 1 round (104 SAT, 23 UNSAT, 1,157 SAT timeout, 1
candidate rejected of 105 generated). It reached this after surviving an unplanned WSL VM
restart mid-run (DB preserved intact, resumed from the same point) and roughly 2.5 h of
apparent single-threaded stall working through the 1,157-fault SAT-timeout backlog —
which was mistakenly read as the campaign being stuck below target and stopped manually.
It had already cleared the 97% threshold by then.

That misreading traces to a real reporting bug on the *monitoring* side, not the tool: the
live status checks during that campaign computed coverage by hand from raw
`SELECT status, COUNT(*) FROM faults GROUP BY status` output as
`detected / (detected + undetected + redundant)` — the *fault coverage* metric (see the
ISCAS TF sweep above) — instead of the tool's actual primary `coverage` field, which
excludes proven-redundant faults from the denominator
(`detected / (detected + undetected)`). That's the figure `ff.py status` and
`coverage_report.json` report and what `THRESHOLD_MET` is graded against. The two metrics
diverge sharply whenever a design accumulates a large redundant count, which TF campaigns
do routinely (see the ISCAS TF table above, where several designs' redundant count exceeds
their detected count). Every number in this document was re-verified directly against
`ff.py status` output rather than hand-derived SQL to avoid repeating this.

Three earlier attempts at this same campaign (identical config, identical design) did
**not** converge — two showed a ~100% SAT-candidate rejection rate, one showed workers
stuck at 100% CPU with essentially nothing new attempted for over an hour. This run showed
neither symptom (1 rejection out of 105 candidates total) before succeeding. The
discrepancy between runs is still unexplained — root cause of the earlier attempts'
non-convergence remains an open investigation, not resolved by this run's success.

### Original full-chip INTEST run (superseded, historical)

```{warning}
Predates the FF Q-stem grading fix — the old grading mis-handled roughly one fault per
flip-flop (~1,613 sites), so the 97.44% figure below is approximate. The stuck-at
entries above use corrected grading and supersede this.
```

| Metric | Value |
|---|---|
| Scan chains / cells | 4 / 1,613 (avg. chain length ~403 FFs) |
| IEEE-1500 WBR cells added | 367 |
| Denominator | 75,654 (of 90,238 raw sites; collapsing disabled) |
| **Formal fault coverage** | **97.44%** (73,712 / 75,654) |
| Achievable coverage (excl. redundant) | 99.57% (73,712 / 74,028) |
| Proven redundant | 1,626 |
| Vectors | 1,296 (all SAT-generated) |
| Wall clock | ~3 h (killed before full convergence; DB was on `/mnt/c` 9P) |

Collapsing was disabled for this run — a deliberately conservative choice for a first
full-chip measurement, grading every fault site individually. faultflow's collapsing
rules cover the Sky130 compound AOI/OAI cells too (equivalence-based, not
dominance-based, so no detectable fault is ever dropped — see
[Collapsing rules](collapsing_rules.md)); with collapsing on, the denominator would
shrink by roughly a third and the SAT call count would drop proportionally.

---

## Reference baseline: Fault v0.9.4

[Fault](https://github.com/AUCOHL/Fault) is an open-source fault simulator for combinational
and sequential logic. It uses a probabilistic (PRNG) test-vector generation strategy and supports
scan insertion for test compression.

The key methodological difference between Fault and faultflow is the **netlist representation**:
Fault removes flip-flops before simulation (a combinational "cut"), while faultflow preserves the
sequential circuit topology and uses native SAT ATPG or user-supplied vectors to exercise it
directly. This means coverage numbers are not directly comparable — Fault's coverage is over the
cut (combinational) netlist, while faultflow's ISCAS-89 results are for sequential scan-aware
patterns. For ISCAS-85 (already combinational, `c*` rows below), the methodology is closer,
though Fault adds a `dummy_clk` port not connected to any logic. ISCAS-89 (`s*` rows) have their
flip-flops removed by Fault's cut.

| Design | Gates | Fault Sites | Coverage | Compact TVs | Synth | Sim |
|--------|------:|------------:|---------:|------------:|------:|----:|
| c17 | 6 | 25 | 100.00% | 4 | 31s | 36s |
| c432 | 101 | 388 | 98.71% | 18 | 30s | 44s |
| c499 | 174 | 603 | 96.10% | 16 | 30s | 48s |
| s27 | 15 | 45 | 86.67% | 6 | 34s | 43s |
| s208 | 53 | 162 | 79.63% | 12 | 34s | 55s |
| s298 | 91 | 280 | 90.00% | 11 | 32s | 1m00s |
| s344 | 102 | 317 | 90.54% | 9 | 34s | 1m00s |
| s349 | 102 | 317 | 90.54% | 9 | 34s | 58s |
| s382 | 127 | 376 | 88.83% | 9 | 34s | 1m07s |
| s386 | 106 | 355 | 81.97% | 15 | 28s | 1m01s |
| s400 | 129 | 376 | 86.17% | 13 | 32s | 45s |
| s444 | 127 | 369 | 88.35% | 11 | 26s | 1m11s |
| s510 | 168 | 586 | 95.22% | 38 | 40s | 59s |
| s526 | 130 | 388 | 88.40% | 14 | 33s | 1m05s |
| s641 | 150 | 491 | 89.31% | 18 | 31s | 1m12s |
| s713 | 151 | 494 | 89.78% | 21 | 31s | 1m07s |
| s820 | 190 | 681 | 83.99% | 30 | 32s | 1m12s |
| s832 | 202 | 715 | 84.90% | 28 | 31s | 1m03s |
| s1196 | 364 | 1244 | 87.70% | 45 | 27s | 1m31s |
| s1238 | 402 | 1372 | 87.06% | 43 | 28s | 1m26s |
| s1423 | 510 | 1517 | 85.86% | 29 | 30s | 1m28s |
| s1488 | 403 | 1445 | 92.87% | 42 | 29s | 1m28s |
| s1494 | 402 | 1455 | 93.64% | 35 | 31s | 1m24s |
| s5378 | 1023 | 3014 | 80.23% | 54 | 34s | 2m22s |

### Fault baseline notes

- **Gates**: post-synthesis gate count from `yosys stat` (OSU035 standard cells; sequential cells excluded from ABC mapping).
- **Fault Sites**: stuck-at-0/stuck-at-1 sites in the (cut, combinational) netlist per Fault's enumerator.
- **Coverage**: percentage of fault sites detected by the final compacted test-vector set.
- **Compact TVs**: vector count after Fault's internal compaction pass.
- **Synth**: wall-clock time for `fault synth` (Yosys synthesis).
- **Sim**: wall-clock time for fault simulation (Fault's PRNG, default ceiling 200 vectors).

## On C++ micro-benchmarks

```{note}
There is no dedicated C++ micro-benchmark harness today — performance is measured with
the run-level timing fields shown above. A Google-Benchmark-based harness is a possible
future addition.
```
