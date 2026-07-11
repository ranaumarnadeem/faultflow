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

## faultflow results — Sky130 HD, native SAT ATPG

Complete sweep across ISCAS-85 and ISCAS-89. Technology: Sky130 HD
(`sky130_fd_sc_hd`). All circuits use scan insertion, structural chain validation,
progressive CaDiCaL SAT ATPG, and post-ATPG compaction. Wall time is end-to-end
(scan insert + check\_scan + ATPG+fault-sim + compaction). ATPG and fault simulation
are interleaved per vector and cannot be separated from the run logs.

Fault model abbreviations: **SA** = stuck-at (SA0/SA1), **TF** = transition fault
(broadside LOC scan). Vectors shown as raw SAT count → post-compaction count.
† = STALLED (SAT exhausted before target; remaining faults are likely redundant).

### ISCAS-85 (combinational, no scan)

| Circuit | SA denom | SA cov% | SA vecs | SA wall | TF denom | TF cov% | TF vecs | TF wall |
|---------|--------:|--------:|--------:|--------:|---------:|--------:|--------:|--------:|
| c17     |      28 | 100.00% |   31→9  |      9s |       28 | 100.00% |   64→11 |      7s |
| c432    |     570 | 100.00% |  88→50  |     10s |      570 | 100.00% | 132→101 |     12s |
| c499    |   1,022 | 100.00% |  96→63  |     12s |    1,022 | 100.00% | 220→165 |     22s |

### ISCAS-89 (sequential, scan SAT ATPG)

Per-phase timing — Check: structural scan chain validation. ATPG+Sim: SAT solve +
bit-parallel fault grading. Compact: post-ATPG vector minimisation.

#### Stuck-at (SA)

| Circuit  | Denom  | Det    | Cov%    | Vecs (raw→cmp) | Check  | ATPG+Sim | Compact | Wall    |
|----------|-------:|-------:|--------:|---------------:|-------:|---------:|--------:|--------:|
| s27      |     64 |     64 | 100.00% |        55→10   |   0.2s |     4.5s |    0.2s |     5s  |
| s208     |    242 |    242 | 100.00% |        85→28   |   0.5s |     8.8s |    0.3s |    10s  |
| s298     |    382 |    378 |  98.95% |        66→30   |   0.2s |    15.4s |    0.3s |    16s  |
| s344     |    502 |    502 | 100.00% |        78→31   |   0.2s |    14.8s |    0.5s |    16s  |
| s349     |    502 |    502 | 100.00% |        78→31   |   0.1s |     7.8s |    0.3s |     8s  |
| s382     |    586 |    586 | 100.00% |        81→34   |   0.4s |     9.5s |    0.6s |    11s  |
| s386     |    536 |    536 | 100.00% |       103→46   |   0.1s |    35.7s |    1.0s |    37s  |
| s400     |    582 |    580 |  99.66% |        85→34   |   0.2s |    10.7s |    0.7s |    12s  |
| s420     |    562 |    562 | 100.00% |       111→54   |   0.2s |    32.2s |    0.5s |    33s  |
| s444     |    588 |    586 |  99.66% |        90→40   |   0.2s |     8.9s |    0.3s |     9s  |
| s510     |    820 |    820 | 100.00% |        88→49   |   0.3s |    11.8s |    1.0s |    13s  |
| s526     |    628 |    628 | 100.00% |        87→42   |   0.4s |    14.4s |    0.7s |    16s  |
| s526n    |    630 |    630 | 100.00% |        90→42   |   0.3s |    16.8s |    0.6s |    18s  |
| s641     |    746 |    746 | 100.00% |        92→49   |   0.3s |    24.7s |    0.3s |    25s  |
| s713     |    708 |    708 | 100.00% |        90→45   |   0.2s |    15.0s |    0.8s |    16s  |
| s820     |  1,025 |  1,020 |  99.51% |       134→68   |   0.2s |    30.1s |    0.5s |    31s  |
| s832     |  1,078 |  1,076 |  99.81% |       124→66   |   0.2s |    26.4s |    0.5s |    27s  |
| s838     |    764 |    758 |  99.22% |        91→38   |   0.4s |    15.9s |    0.6s |    17s  |
| s1196    |  1,954 |  1,948 |  99.69% |       175→114  |   0.4s |    35.3s |    3.7s |    39s  |
| s1238    |  1,945 |  1,912 |  98.30% |       187→122  |   0.6s |    33.9s |    1.3s |    36s  |
| s1423    |  2,373 |  2,370 |  99.87% |       130→91   |   0.7s |    35.5s |    1.0s |    37s  |
| s1488    |  2,064 |  2,060 |  99.81% |       142→101  |   0.2s |    20.6s |    0.8s |    22s  |
| s1494    |  2,144 |  2,138 |  99.72% |       140→90   |   0.4s |    18.5s |    0.9s |    20s  |
| s5378    |  4,563 |  4,546 |  99.63% |       287→217  |   1.7s |    59.1s |    4.9s |  1m 6s  |
| s9234    |  3,852 |  3,838 |  99.64% |       219→160  |   1.6s |    45.1s |    4.0s |    51s  |
| s15850   | 13,581 | 13,472 |  99.20% |       557→384  |  21.8s |  6m 40s  |   44.9s | 7m 47s  |

#### Transition faults (TF, broadside LOC scan)

| Circuit  | Denom  | Det    | Cov%    | Vecs (raw→cmp) | Check  | ATPG+Sim | Compact  | Wall     |
|----------|-------:|-------:|--------:|---------------:|-------:|---------:|---------:|---------:|
| s27      |     28 |     24 |  85.71%†|        49→5    |   0.1s |    <1s   |     1.6s |      2s  |
| s208     |    180 |    178 |  98.89% |        72→26   |   0.1s |    <1s   |     1.3s |      2s  |
| s298     |    291 |    283 |  97.25% |        86→29   |   0.3s |    <1s   |     3.3s |      2s  |
| s344     |    434 |    430 |  99.08% |        95→39   |   0.1s |    <1s   |     3.7s |      2s  |
| s349     |    434 |    430 |  99.08% |        95→39   |   0.3s |    <1s   |     2.6s |      2s  |
| s382     |    413 |    407 |  98.55% |        96→35   |   0.3s |     1.6s |     2.4s |      4s  |
| s386     |    348 |    306 |  87.93%†|       111→36   |   0.2s |    <1s   |     3.4s |      3s  |
| s400     |    410 |    401 |  97.81% |       101→38   |   0.1s |     0.1s |     2.1s |      2s  |
| s420     |    420 |    416 |  99.05% |       102→53   |   0.2s |    <1s   |     3.9s |      2s  |
| s444     |    418 |    403 |  96.41% |       105→43   |   0.4s |    <1s   |     3.2s |      1s  |
| s510     |    639 |    596 |  93.27% |       123→58   |   0.5s |    <1s   |     4.1s |      1s  |
| s526     |    398 |    386 |  96.99% |        96→39   |   0.2s |    <1s   |     3.0s |      2s  |
| s526n    |    404 |    390 |  96.54% |       101→40   |   0.9s |    29.6s |     2.6s |     33s  |
| s641     |    428 |    401 |  93.69% |       128→50   |   0.2s |    25.2s |     4.0s |     30s  |
| s713     |    399 |    373 |  93.48% |       129→55   |   0.3s |    41.2s |     8.5s |     50s  |
| s820     |    658 |    614 |  93.31% |       138→61   |   0.1s |    33.8s |     5.0s |     39s  |
| s832     |    708 |    662 |  93.50% |       148→60   |   0.4s |    31.1s |     5.7s |     37s  |
| s838     |    524 |    514 |  98.09% |       113→64   |   0.4s |    54.8s |     8.0s |  1m 3s   |
| s1196    |    419 |    408 |  97.37% |       107→36   |   0.2s |    41.4s |     9.4s |     51s  |
| s1238    |    420 |    406 |  96.67% |       107→41   |   0.2s |    42.8s |     8.9s |     52s  |
| s1423    |  1,809 |  1,762 |  97.40% |       212→130  |   0.6s |  1m 31s  |    20.9s |  1m 52s  |
| s1488    |  1,479 |  1,346 |  91.01% |       215→112  |   0.2s |    32.9s |    12.9s |     46s  |
| s1494    |  1,797 |  1,730 |  96.27% |       207→125  |   0.2s |    30.4s |    11.7s |     42s  |
| s5378    |  3,319 |  3,264 |  98.34% |       432→323  |   1.8s |  3m 3s   |  2m 7s   |  5m 12s  |
| s9234    |  3,405 |  3,355 |  98.53% |       372→276  |   1.5s |  2m 16s  |  1m 27s  |  3m 44s  |
| s15850   |  9,576 |  9,163 |  95.69% |       744→564  |  16.5s | 22m 29s  | 30m 3s   | 52m 48s  |

† Stalled: SAT exhausted without reaching target. Remaining faults are likely
redundant or require longer sequential unrolling.

---

## PicoRV32a — Full-Chip Scan INTEST (Sky130 HD)

PicoRV32a is an open-source RISC-V RV32IMC CPU. It is the largest design faultflow has been
exercised on to date and the first full-chip scan INTEST result with IEEE-1500 WBR wrapping.

### Design and scan infrastructure

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
