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
[Examples](user_guide/examples.md) with `/usr/bin/time`:

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

## faultflow native scan ATPG — ISCAS-85/89 sweep

faultflow's own numbers across the full shipped ISCAS corpus (30 designs), run with
the native SAT ATPG on the Sky130 HD netlists. Unlike the Fault baseline below (which
grades a combinational *cut* of the sequential circuits), these are **sequential
scan-aware** results: flip-flops are stitched into scan chains and exercised through
scan load/unload, so the coverage is over the real sequential fault set.

**Method.** Scan insertion at **≤10 flip-flops per chain**; native SAT ATPG with 4
workers; single-tier 2 s per-fault SAT timeout; `max_rounds = 6`; incremental-SAT
(IFC) **off**; fault collapsing off; DB on a native ext4 path (not `/mnt/c`). Each
design is independently wall-clock-guarded so one slow design cannot stall the sweep.

### ISCAS-85 (combinational)

| Design | Cells | Coverage | Wall |
|---|---:|---:|---:|
| c17 | 3 | 100.00% | 10.5 s |
| c432 | 65 | 100.00% | 11.9 s |
| c499 | 160 | 100.00% | 17.1 s |

### ISCAS-89 (sequential, scan)

| Design | Cells | FFs | Chains | Coverage | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
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

### ISCAS-85 (combinational)

| Design | Cells | Coverage | Fault cov. | Redundant | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
| c17 | 3 | 100.00% | 100.00% | 0 | 13.7 s | COMPLETE |
| c432 | 65 | 100.00% | 99.30% | 4 | 18.9 s | COMPLETE |
| c499 | 160 | 100.00% | 100.00% | 0 | 31.7 s | COMPLETE |

### ISCAS-89 (sequential, scan)

| Design | Cells | FFs | Chains | Coverage | Fault cov. | Redundant | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---:|---:|---|
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

---

## PicoRV32a — Full-Chip Scan INTEST (Sky130 HD)

PicoRV32a is an open-source RISC-V RV32IMC CPU. It is the largest design faultflow has been
exercised on to date and the first full-chip scan INTEST result with IEEE-1500 WBR wrapping.

### Plain stuck-at scan ATPG (audit-fixed core, ≤10 FF/chain, random→SAT switch)

A fresh non-WBR run on the audit-fixed core exercising the ≤10 FF/chain rule, 6 workers,
IFC off, fault collapsing on, and the `atpg.random_stop_coverage = 85%` random→SAT
switch:

| Metric | Value |
|---|---|
| Cells / flip-flops | 12,601 / 1,613 |
| Scan chains | **162** (~10 FF/chain) · 1,613 scan cells |
| **Test coverage** | **98.67%** (57,513 / 58,286 testable) |
| Fault coverage | 96.4% (of 59,678 in-scope; collapsing on) |
| Proven redundant (UNSAT) | 1,392 |
| Undetected | 773 |
| Vectors | **1,256** (compacted from 1,696) |
| Reverse-compaction time | **1,619 s** (~27 min, single-threaded, 1,696 → 1,256) |
| Terminal | THRESHOLD_MET (crossed 95% in round 1) |
| Random→SAT switch | stopped random fill at 85% after **31 / 64 vectors** |

The random→SAT switch is the notable behaviour: rather than grading all 64 random
vectors, it detected the easy 85% with 31 vectors and handed the hard remainder to
SAT (2,252 SAT-detected, 1,392 proven redundant).

**Reverse vector compaction is a known post-ATPG scaling cost.** Shrinking the
1,696-vector set to 1,256 took **1,619 s (~27 min), single-threaded, with essentially
zero I/O** — the pass runs entirely after coverage is final, so it does not affect the
reported numbers, only the wall clock. The cost grows super-linearly (≈O(N²)) with the
raw vector count on large fault populations; it is left as-is for now and recorded here
so future runs can be compared against it.

### Transition-fault scan ATPG (LOC / broadside, first recorded transition result)

The first faultflow transition-fault benchmark: same audit-fixed core, same ≤10 FF/chain
scan config (162 chains), 6 workers, IFC off, `atpg.random_stop_coverage = 85%`
random→SAT switch, **launch-on-capture (broadside) two-frame transition faults**.
Fault collapsing must be off for transition ATPG (config hard-error otherwise), so this
run is against all 74,314 in-scope transition fault sites, uncollapsed.

| Metric | Value |
|---|---|
| Launch model | LOC / broadside (two-frame, scan capture) |
| In-scope transition faults | **74,314** |
| **Detected** | **66,740** |
| **Fault coverage** | **89.81%** (66,740 / 74,314) |
| Proven redundant (UNSAT, 2-frame) | **3,861** |
| **Test coverage** (excl. redundant) | **94.73%** (66,740 / 70,453) |
| Undetected | 3,713 |
| Vectors | **1,598** (uncompacted — compaction intentionally skipped, see below) |
| Rounds completed | 2 of 20 (round 3 stopped in flight, no new commits) |
| Wall clock | ~10.5 h, run manually stopped after round 2's clean close |

**Run was stopped by hand after round 2, not left to converge or compact.** Round 1's
SAT wave contained a long, slow tail (~220 faults each riding the full 60 s timeout
tier) that took several hours to drain — the classic broadside signature of a design
with a large functionally-untestable transition-fault population. Round 2 closed
cleanly at the numbers above; round 3 was still mid-wave with **zero new commits**
after ~800 s when the run was killed, so it contributes nothing to the reported
numbers. Two-frame reverse compaction (expensive on the stuck-at run, see above) was
skipped entirely for this measurement — the 1,598 vectors above are the raw SAT-wave
output, uncompacted.

**Reading the result:** fault coverage (89.8%) is well short of the stuck-at run's
96.4%, but that gap is largely *not* a weakness in the ATPG — it is the redundant
block (3,861 and still growing when stopped) representing transitions that are simply
not launchable/observable in one functional capture cycle from a scan-loadable state.
Test coverage (94.7%, i.e. coverage of the *testable* fault population) is a fairer
comparison point to the stuck-at number and was about to cross the 95% target when the
run was stopped.

### Design and scan infrastructure (original full-chip INTEST run)

```{warning}
The coverage numbers in this "original full-chip INTEST run" block predate the
FF Q-stem grading fix and should not be cited as-is. The old grading mis-handled
roughly one fault per flip-flop (~1,613 sites), so the 97.44% figure below is
approximate. The **plain stuck-at scan ATPG (audit-fixed core)** result at the
top of this section (98.67%) uses the corrected grading and supersedes it; the
raw pre-fix data is in `benchmark_picorv32a.md`, which carries the same caveat.
```

| Metric | Value |
|---|---|
| Technology | Sky130 HD (`sky130_fd_sc_hd`) |
| Combinational gates | 10,989 |
| Flip-flops | 1,613 (610 × dfxtp_1 + 1,003 × edfxtp_1) |
| Total cells (pre-wrap) | 12,602 |
| IEEE-1500 WBR cells added | 367 |
| Scan chains | **4** |
| Scan cells | **1,613** |
| Average chain length | ~403 FFs |

### Fault statistics

Fault model: stuck-at SA0/SA1. Fault collapsing **disabled** (see
[why](#why-fault-collapsing-was-not-used-on-picorv32a) below).

| Category | Count |
|---|---|
| Total raw fault sites | 90,238 |
| Denominator (active, in-scope) | **75,654** |
| Excluded — scan shift-path internal | 6,198 |
| Excluded — clock nets | 3,962 |
| Excluded — scan chain infrastructure | 2,944 |
| Excluded — WBR boundary (blackboxed) | 1,138 |
| Excluded — reset nets | 342 |

### ATPG results

| Metric | Value |
|---|---|
| Detected | **73,712** |
| Proven redundant (UNSAT) | 1,626 |
| Unresolved at termination | 316 (0.42% of denominator) |
| Denominator resolved | 99.58% |
| **Formal fault coverage** | **97.44%** (73,712 / 75,654) |
| Achievable coverage (excl. redundant) | **99.57%** (73,712 / 74,028) |
| Test vectors | **1,296** (all SAT-generated, 0 random) |
| ATPG engine | Native SAT (CaDiCaL), 4 parallel workers |
| Timeout schedule | 2 s → 10 s → 60 s escalating |
| Fault ordering | Cone-of-influence size (small first) |
| Scan chain validation | 459.4 s |
| ATPG wall clock | **~3 h** (killed before full convergence; DB on `/mnt/c` 9P) |

### Why fault collapsing was not used on PicoRV32a

Fault collapsing removes equivalent fault sites from the denominator, reducing vector
count. This run was made with collapsing **disabled** so every fault site was graded
individually (all 75,654) — a deliberately conservative choice for a first full-chip
measurement.

Collapsing is off by default but is not limited to simple primitives: faultflow's rules
cover the Sky130 compound AOI/OAI cells (`o21ai`, `a21oi`, `o22ai`, `a22o`, …) too, via
equivalence classes derived exhaustively (identical detecting-vector sets) and
cross-checked against the simulator — see [Collapsing rules](collapsing_rules.md). The
rules are equivalence-based, not dominance-based, so no detectable fault is ever dropped.

With collapsing enabled the denominator would shrink by roughly a third, the SAT call
count would drop proportionally, and the formal coverage % would be approximately the
same or slightly higher.

For the complete raw data and timing breakdown, see `benchmark_picorv32a.md`.

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
patterns. For ISCAS-85 (already combinational), the methodology is closer, though Fault adds a
`dummy_clk` port not connected to any logic.

### ISCAS-85 (Combinational)

Pure combinational logic (Fault injects a `dummy_clk` port into RTL before synthesis).

| Design | Gates | Fault Sites | Coverage | Compact TVs | Synth | Sim |
|--------|------:|------------:|---------:|------------:|------:|----:|
| c17 | 6 | 25 | 100.00% | 4 | 31s | 36s |
| c432 | 101 | 388 | 98.71% | 18 | 30s | 44s |
| c499 | 174 | 603 | 96.10% | 16 | 30s | 48s |

### ISCAS-89 (Sequential)

Sequential circuits with flip-flops removed by Fault's combinational cut. Coverage is over the
resulting cut (combinational) netlist, not the original sequential circuit.

| Design | Gates | Fault Sites | Coverage | Compact TVs | Synth | Sim |
|--------|------:|------------:|---------:|------------:|------:|----:|
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

For raw data and methodology, see `benchmark_fault.md` in the repository root.

## On C++ micro-benchmarks

```{note}
There is no dedicated C++ micro-benchmark harness today — performance is measured with
the run-level timing fields shown above. A Google-Benchmark-based harness is a possible
future addition.
```
