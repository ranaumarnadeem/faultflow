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
