# Benchmarking

faultflow's benchmark data covers two things: the **circuits** it's exercised on, and the
**timing fields** every run records.

## Benchmark circuits

| Suite | Circuits | Location |
|---|---|---|
| ISCAS-85 (combinational) | `c17`, `c432`, `c499` | `tests/benchmarks/iscas85/synth/` (OSU035), `…/synth_sky130/` (Sky130) |
| ISCAS-89 (sequential) | `s27` … `s15850` (28 circuits) | `tests/benchmarks/iscas89/synth_sky130/` |

Pre-synthesized (no Yosys re-run needed). `examples/picorv32_synth/` is the larger stress test.

## Timing data every run records

| Report field | Meaning |
|---|---|
| `atpg_seconds` | Time in SAT ATPG (`atpg_generation_seconds`) |
| `fault_sim_seconds` | Time in bit-parallel fault simulation (`fault_simulation_seconds`) |
| `total_sim_seconds` | End-to-end campaign time |
| `atpg_rounds` | Progressive-loop round count |
| `atpg_sat` / `atpg_unsat` / `atpg_timeout` / `atpg_unknown` | Per-fault SAT outcome counts |

## Measuring throughput

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

`status` reports `atpg_seconds`/`fault_sim_seconds`; `/usr/bin/time` captures total process
time and peak memory.

## Comparing against other tools

- **Quaigh** — `[atpg] tool = quaigh` converts to BENCH via `nl2bench` and runs `quaigh atpg`.
  See [External tools](external_tools.md).
- **Transition vs. stuck-at** — run the same circuit with `model = stuck_at` then
  `model = transition` for a side-by-side.

## faultflow native scan ATPG — ISCAS-85/89 stuck-at sweep

**Method:** ≤10 FF/chain scan; native SAT ATPG, 4 workers; 2 s per-fault SAT timeout;
`max_rounds=6`; IFC off; collapsing off; DB on native ext4. `c17`/`c432`/`c499` are
combinational (no scan) — FFs/Chains read "—".

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

¹ `s13207`: `sim --scan requires full scan: ineligible FFs remain` — a bit-sliced FF
(`$auto$ff.cc:…:slice`) scan insertion doesn't cover.

## faultflow native scan ATPG — ISCAS-85/89 transition-fault (LOC) sweep

**Method:** same ≤10 FF/chain scan; native SAT ATPG, 4 workers; 2 s→10 s escalating
timeout; `max_rounds=6`; IFC off; collapsing off (required for the transition model); DB
on native ext4. `c17`/`c432`/`c499` run `-tf broadside` without `-scan`.

**Coverage** = detected / testable (excludes SAT-proven-redundant). **Fault cov.** =
detected / full in-scope (testable + redundant).

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

¹ `s5378`: coverage-report JSON missing (hit its 800 s budget mid-write); numbers
recovered from the campaign DB.
² `s13207`: same scan-eligibility rejection as the stuck-at sweep (footnote ¹ above).
³ `s15850`: killed mid-round-1 (9,742/9,926 faults into that round's SAT wave) at the
1,200 s budget — an in-flight snapshot, not a final result.

### 2026-08 re-validation run (single-tier SAT timeout, `random_vectors=64`)

Spot-check with faultflow's current default single-tier per-fault SAT timeout (vs. the
2 s→10 s escalating schedule above) and a smaller vector budget.

| Design | FFs | Chains | Coverage | Redundant | Wall | Terminal |
|---|---:|---:|---:|---:|---:|---|
| c17 | — | — | 100.00% | 0 | 7.5 s | COMPLETE |
| c432 | — | — | 100.00% | 4 | 16.0 s | COMPLETE |
| c499 | — | — | 100.00% | 0 | 26.8 s | COMPLETE |
| s27 | 3 | 1 | 100.00% | 40 | 12.5 s | COMPLETE |
| s15850 | 559 | 56 | 99.05% | 4,837 | 7,041.9 s | COMPLETE |
| iiravg | 16 | 2 | 100.00% | 256 | 175.3 s | COMPLETE |

---

## faultflow native scan ATPG — DSP filter designs

Gisselquist Technology `dspfilters` blocks. `genericfir_small` = the `genericfir` core at
8 taps/8-bit samples (vs. its default 128 taps/12-bit — the full-size core is run
separately, stuck-at only).

**Native SAT** = 4 workers, `threshold=99.0`, `max_rounds=100`, collapsing on.
**Random→SAT hybrid** (`ff_rerun_dev` campaign, 6 workers) = random-fill to 97%, then
switch to SAT at 90% random coverage; only hybrid finals are shown — the pure-random
phase's raw figures used an inconsistent coverage formula and were discarded before
verification (see the coverage-formula note on the PicoRV32a TF table below).

| Design | Campaign | Model | FFs | Chains | Coverage | Redundant | Vectors | Wall | Terminal |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| iiravg | Native SAT | SA | 16 | 2 | 100.00% | — | 30 | 7.0 s | COMPLETE |
| iiravg | Random→SAT hybrid | SA | 16 | 2 | 100.00% | 30 | 36 | 16.7 s | COMPLETE |
| iiravg | Random→SAT hybrid | TF | 16 | 2 | 100.00% | 256 | 80 | 774.2 s | COMPLETE |
| boxcar | Native SAT | SA | 1,130 | 113 | 98.926% | — | 288 | 336.1 s | THRESHOLD_MET |
| boxcar | Random→SAT hybrid | SA | 1,130 | 113 | 100.00% | 0 | 1,100 | 1,243.3 s | COMPLETE |
| boxcar | Random→SAT hybrid | TF | 1,130 | 113 | 91.98%¹ | 5,039 | — | 26,494.8 s | KILLED¹ |
| genericfir_small | Native SAT | SA | 471 | 48 | 99.981% | — | 287 | 590.0 s | MAX_ROUNDS |
| genericfir_small | Native SAT | TF | 471 | 48 | 99.848% | — | 351 | — | THRESHOLD_MET |
| genericfir_small | Random→SAT hybrid | SA | 471 | 48 | 100.00% | 17 | 344 | 338.7 s | COMPLETE |
| genericfir_small | Random→SAT hybrid | TF | 471 | 48 | 100.00% | 3,682 | 261 | 4,012.4 s | COMPLETE |

¹ Killed after ~90 min of flat detection at the ~7.4 h mark; 91.98% is a snapshot
(21,106/22,949 detected/testable), not a final result.

---

## PicoRV32a — Full-Chip Scan INTEST (Sky130 HD)

Largest design exercised to date; first full-chip scan INTEST with IEEE-1500 WBR
wrapping. Same audit-fixed core, ≤10 FF/chain scan (162 chains), for all three runs
below.

### Stuck-at scan ATPG

| Metric | Native SAT (uncapped) | Random→SAT hybrid | 2026-08 rerun |
|---|---:|---:|---:|
| Workers | 4 | 6 | 6 |
| Fault collapsing | off | on | — |
| Random→SAT switch | none (pure SAT) | `random_stop_coverage=85%`, stopped 31/64 vectors | random-fill to 90%, then SAT |
| Denominator | 72,652 (uncollapsed) | 58,286 (collapsed) | 87,536 (testable) |
| **Coverage** | **99.785%** (72,496 detected) | **98.67%** (57,513 detected) | **99.976%** (87,515 detected) |
| Fault coverage | 97.55% (of 74,314) | 96.4% (of 59,678) | — |
| Proven redundant (UNSAT) | 1,662 | 1,392 | 918 |
| Undetected | 156 | 773 | 21 |
| Vectors | 1,164 (uncompacted) | 1,256 (compacted from 1,696) | 1,784 |
| Reverse-compaction time | — | 1,619 s (~27 min) | — |
| ATPG + fault-sim time | 202.3 s + 4,185.5 s | — | 198.4 s + 6,839.4 s |
| Wall clock | — | — | 13,913.8 s (~3 h 52 min) |
| Terminal | THRESHOLD_MET | THRESHOLD_MET | — |

¹ Native SAT's coverage jump over the hybrid run traces to a since-fixed
witness-truncation bug: a wide-bus PI collapsed to a single bit (bit 0) throughout ATPG,
permanently blocking any fault only detectable via a non-front bit of that port.

### Transition-fault scan ATPG (launch-on-capture / broadside)

Collapsing off for all three (hard requirement for the transition model).

| Metric | Native SAT (uncapped) | Random→SAT hybrid | 2026-08 rerun |
|---|---:|---:|---:|
| Workers | 4 | 6 | 6 |
| Denominator | 70,046 (uncollapsed) | 70,453 (uncollapsed) | 85,504 (testable) |
| **Coverage** | **98.89%** (69,271 detected) | 94.73% (66,740 detected) | **98.714%** (84,404 detected) |
| Fault coverage | 93.22% (of 74,314) | 89.81% (of 74,314) | — |
| Proven redundant (UNSAT) | 4,268 | 3,861 | 2,950 |
| Undetected | 775 | 3,713 | 1,100 |
| Vectors | — | 1,598 (uncompacted) | — |
| Rounds | — | 2 of 20 | 1 (104 SAT / 23 UNSAT / 1,157 timeout) |
| Wall clock | ~7 h | ~10.5 h | — |
| Terminal | — | — | THRESHOLD_MET¹ |

¹ **Coverage** excludes proven-redundant faults from the denominator (`ff.py status`'s
primary field, what `THRESHOLD_MET` is graded against). **Fault coverage** includes
them. Hand-deriving from raw `SELECT status, COUNT(*) ... GROUP BY status` SQL computes
the latter and reads lower/more pessimistic — this once caused the 2026-08 rerun to be
misread as stalled below target when it had already cleared threshold. Every number in
this document is read from `ff.py status`/`coverage_report.json`, never hand-derived.

### Original full-chip INTEST run (superseded, historical)

```{warning}
Predates the FF Q-stem grading fix (mis-handled ~1 fault per flip-flop, ~1,613 sites) —
the stuck-at entries above use corrected grading and supersede this.
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
| Wall clock | ~3 h (killed before convergence; DB on `/mnt/c` 9P) |

---

## Hierarchical SoC composition — chip-level EXTEST/INTEST aggregation (Sky130 HD)

Four SoCs composed from the blocks above via `assemble_soc`/`aggregate_project`: each
block wrapped (IEEE-1500 WBR) and INTEST-graded standalone, the composed chip graded
under EXTEST for interconnect-only logic, then combined into one chip-level number via a
disjoint union (`tops_disjoint`, `no_double_count`, `partition_total`,
`handoff_complete` guards — all pass on all four).

| SoC | Blocks | Composed top |
|---|---|---|
| SoC1 | picorv32a + uart | `soc1_glue` |
| SoC2 | serv + uart | `soc2_glue` |
| SoC3 | tiny_aes + uart + soc3_controller | `soc3_glue` |
| SoC4 | iiravg + boxcar + genericfir_small | `soc4_glue` |

### Chip-level aggregate stuck-at coverage

| SoC | Chip denominator | Chip detected | **Chip coverage** | Guards |
|---|---:|---:|---:|---|
| SoC1 (picorv32a+uart) | 70,713 | 70,507 | **99.71%** | 4/4 pass |
| SoC2 (serv+uart) | 8,768 | 8,732 | **99.59%** | 4/4 pass |
| SoC3 (tiny_aes+uart+controller) | 191,263 | 191,203 | **99.97%** | 4/4 pass |
| SoC4 (iiravg+boxcar+genericfir_small) | 40,643 | 40,643 | **100.00%** | 4/4 pass |

### Per-scope breakdown

| SoC | Scope | Kind | Denominator | Detected | Coverage |
|---|---|---|---:|---:|---:|
| SoC1 | picorv32a | block (INTEST) | 65,986 | 65,945 | 99.94% |
| SoC1 | uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| SoC1 | soc1_extest | interconnect (EXTEST) | 953 | 788 | 82.69% |
| SoC2 | serv | block (INTEST) | 4,217 | 4,217 | 100.00% |
| SoC2 | uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| SoC2 | soc2_extest | interconnect (EXTEST) | 777 | 741 | 95.37% |
| SoC3 | tiny_aes | block (INTEST) | 179,354 | 179,354 | 100.00% |
| SoC3 | uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| SoC3 | controller | block (INTEST) | 7,157 | 7,157 | 100.00% |
| SoC3 | soc3_extest | interconnect (EXTEST) | 978 | 918 | 93.87% |
| SoC4 | iiravg | block (INTEST) | 924 | 924 | 100.00% |
| SoC4 | boxcar | block (INTEST) | 18,148 | 18,148 | 100.00% |
| SoC4 | genericfir_small | block (INTEST) | 21,409 | 21,409 | 100.00% |
| SoC4 | soc4_extest | interconnect (EXTEST) | 162 | 162 | 100.00% |

### Scan-pattern retargeting validation

Each block's SAT-ATPG INTEST patterns retargeted through the composed chip's physical
scan chains (native functional chain + WBR boundary ring, driven simultaneously) and
re-verified against the full SoC netlist.

| Block | SoC context | Chain length | Patterns verified |
|---|---|---:|---:|
| iiravg | SoC4 | 16 (2 chains) | **78/78** (exhaustive) |
| boxcar | SoC4 | 1,130 (113 chains) | **30/30** |
| genericfir_small | SoC4 | 471 (48 chains) | **30/30** |
| picorv32a | SoC1 | 1,613 (162 chains) | **2/2** |
| uart | SoC1 | 131 (2 chains) | **5/5** |
| uart | SoC2 | 131 (2 chains) | **5/5** |
| uart | SoC3 | 131 (2 chains) | **5/5** |
| serv | SoC2 | 164 (20 chains) | **10/10** |
| soc3_controller | SoC3 | 411 (45 chains) | **5/5** |
| tiny_aes | SoC3 | 5,568 (600 chains) | **1/1** |

**Zero failures across all 7 blocks / 4 SoCs**, after fixing two bugs in
`faultflow/retarget/transform.py` (both invisible to the prior unit test, whose fixture's
one chain happens to equal `max_chain_length`):

1. Load-side padding was appended *after* the real payload instead of before it — a
   shorter physical register only retains the *last* N bits shifted in, so trailing
   padding evicted real stimulus before the capture edge.
2. A block's own exported pattern is pre-padded to *that block's own* manifest
   `max_chain_length` whenever its chains aren't uniform (e.g. genericfir_small: 39
   chains of length 10, 9 of length 9). Retargeting assumed array length always equalled
   true chain length and silently misread the pre-padded array.

---

## Reference baseline: Fault v0.9.4

[Fault](https://github.com/AUCOHL/Fault) is an open-source fault simulator using PRNG
vector generation. It removes flip-flops before simulation (a combinational cut), while
faultflow preserves sequential topology and uses SAT ATPG directly — so coverage numbers
aren't directly comparable except for `c17`/`c432`/`c499` (already combinational).
ISCAS-89 (`s*`) rows have their flip-flops removed by Fault's cut.

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

- **Gates**: post-synthesis `yosys stat` count (OSU035 standard cells; sequential cells excluded from ABC mapping).
- **Fault Sites**: SA0/SA1 sites in the cut (combinational) netlist.
- **Coverage**: % of fault sites detected by the final compacted vector set.
- **Compact TVs**: vector count after Fault's internal compaction.
- **Synth**: wall-clock for `fault synth` (Yosys synthesis).
- **Sim**: wall-clock for fault simulation (Fault's PRNG, default 200-vector ceiling).

## On C++ micro-benchmarks

```{note}
No dedicated C++ micro-benchmark harness today — performance is measured with the
run-level timing fields above. A Google-Benchmark-based harness is a possible future
addition.
```
