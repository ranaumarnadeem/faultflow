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
| Terminal | THRESHOLD_MET (crossed 95% in round 1) |
| Random→SAT switch | stopped random fill at 85% after **31 / 64 vectors** |

The random→SAT switch is the notable behaviour: rather than grading all 64 random
vectors, it detected the easy 85% with 31 vectors and handed the hard remainder to
SAT (2,252 SAT-detected, 1,392 proven redundant). Note: reverse **vector compaction**
of the 1,696 → 1,256 set took ~27 min single-threaded — a known post-ATPG scaling cost
on a 60k-fault campaign (the coverage number is final before compaction runs).

### Design and scan infrastructure (original full-chip INTEST run)

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

Fault collapsing removes equivalent/dominated fault sites from the denominator, reducing
vector count. faultflow's verified collapsing rules only cover INV, BUF, AND2/NAND2,
OR2/NOR2. Sky130 synthesis produces many AOI/OAI compound cells
(`o21ai`, `a21oi`, `o22ai`, `a22o`, …) where dominance across fanout-split branches has
not been formally verified. Collapsing unverified compound cells can silently inflate
coverage by removing detectable faults. The ATPG was therefore run against all 75,654
individually-enumerated fault sites.

With collapsing enabled (once AOI/OAI rules are verified), the denominator would shrink
by ~30–40%, SAT call count would drop proportionally, and the formal coverage % would
be approximately the same or slightly higher.

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
The project documentation references Google Benchmark for C++ micro-benchmarks, but no
such benchmark target is built today. Performance is currently measured with the
run-level timing fields above. A dedicated micro-benchmark harness is a possible future
addition.
```
