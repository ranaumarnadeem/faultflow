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
netlist  = tests/benchmarks/iscas85/synth/${top}.json
top      = ${top}
cell_lib = cells/osu/osu035.json
liberty  = cells/osu/osu035_stdcells.lib
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

## On C++ micro-benchmarks

```{note}
The project documentation references Google Benchmark for C++ micro-benchmarks, but no
such benchmark target is built today. Performance is currently measured with the
run-level timing fields above. A dedicated micro-benchmark harness is a possible future
addition.
```
