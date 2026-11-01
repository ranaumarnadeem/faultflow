# FaultFlow vs Fault on 16 ISCAS netlists (OSU035)

The run records behind the paper's comparison with the Fault toolchain
(AUCOHL, <https://github.com/AUCOHL/Fault>). `logs/` holds the raw output,
`scripts/` the scripts that produced it. The scripts are kept as run: they
use absolute paths under `~/ffcmp`.

## Setup

- **Machine:** HP EliteBook 840 G11, Intel Core Ultra 5 125U. Runs were in
  WSL2 (Ubuntu 24.04, kernel 6.6.87.2), which sees 10 logical CPUs and 23 GB
  of RAM. One run per design, no other load control.
- **Netlists:** both tools grade the same netlist per design. That is Fault's
  own `synth` + `cut` output on the OSU035 cells (`scripts/prep.sh`).
  FaultFlow reads it after an unused clock port is stripped.
- **Fault:** 0.9.4, built with Nix from `github:AUCOHL/Fault`, with one local
  patch. A `while task.isRunning { usleep(1000) }` wait was added in
  `Sources/Fault/String.swift` to fix a SIGILL race. Settings were
  `-v 500 -r 500 -m 100 --ceiling 5000` (`scripts/run_fault.sh`): 500
  vectors, then 500 more per round, to 100% coverage or 5,000 vectors. Fault
  ran with its default thread pool, one thread per logical CPU.
  - Two ATPG modes were timed: its default pseudo-random generator (PRNG)
    and Quaigh as the generator.
  - Only `fault atpg` is timed, not `synth`, `cut` or `nl2bench`.
- **FaultFlow:** main at 45725df, `workers = 1` (`scripts/run_ff_dyn.sh`).
  It uses random vectors until 85% coverage (at most 1,000), then SAT,
  with dynamic compaction and fault collapsing off. `ff.py init` plus
  `ff.py sim` is timed.
- **Fault lists differ:** Fault puts faults on every gate pin and port,
  FaultFlow on stems and fanout branches. Fault coverage is comparable
  only loosely.
- **Dates:** Fault ran on 2026-10-19 (`logs/fault_sweep.log`) and FaultFlow
  on 2026-10-20 (`logs/ff_sweep_dyn.log`).

## Results

Times are in seconds. "Fault cov." is detected over all faults. FaultFlow's
"test cov." also leaves out the faults SAT proved redundant. Fault does not
classify redundant faults.

| Design | Fault PRNG cov. | Fault PRNG vectors | Fault PRNG time | Fault + Quaigh time | FaultFlow fault cov. | FaultFlow test cov. | FaultFlow vectors | FaultFlow time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| c432 | 99.74% | 18 | 195.1 | 3.2 | 99.22% | 100.00% | 39 | 12.2 |
| c499 | 100.00% | 37 | 84.6 | 2.7 | 100.00% | 100.00% | 48 | 10.6 |
| c880 | 100.00% | 16 | 302.4 | 5.7 | 100.00% | 100.00% | 47 | 10.4 |
| c1355 | 100.00% | 37 | 58.2 | 4.0 | 100.00% | 100.00% | 49 | 9.7 |
| c1908 | 100.00% | 36 | 144.0 | 10.8 | 99.75% | 100.00% | 78 | 12.2 |
| c2670 | 82.95% | 24 | 733.4 | 13.3 | 99.74% | 100.00% | 92 | 42.2 |
| c3540 | 99.87% | 49 | 645.7 | 13.4 | 99.65% | 100.00% | 139 | 15.7 |
| c5315 | 99.95% | 26 | 1003.2 | 19.9 | 99.85% | 100.00% | 88 | 21.8 |
| s27 | 86.67% | 6 | 2.7 | 1.2 | 90.32% | 100.00% | 7 | 6.2 |
| s298 | 89.15% | 9 | 118.0 | 1.3 | 92.67% | 100.00% | 23 | 4.9 |
| s344 | 90.60% | 7 | 140.4 | 1.1 | 93.32% | 100.00% | 21 | 7.3 |
| s382 | 88.71% | 9 | 130.5 | 1.8 | 92.73% | 100.00% | 32 | 6.2 |
| s526 | 90.00% | 12 | 156.7 | 1.6 | 93.22% | 100.00% | 31 | 7.9 |
| s820 | 98.57% | 38 | 225.6 | 6.4 | 99.12% | 100.00% | 87 | 18.3 |
| s1238 | 94.68% | 52 | 550.1 | 11.6 | 96.20% | 100.00% | 161 | 32.6 |
| s1423 | 89.64% | 28 | 659.7 | 13.6 | 93.19% | 100.00% | 56 | 21.0 |
| **Total** | | **404** | **5150.3** | **111.6** | | | **998** | **239.2** |

- **Speed against Fault's PRNG generator:** the ratio of total times is
  5150.3 / 239.2 = 21.5x. The geometric mean of the per-design ratios is
  14.7x, ranging from 0.45x (s27) to 46.0x (c5315).
- **Speed against Fault with Quaigh:** that flow took 111.6 s in total, so
  FaultFlow is 2.1x slower. Quaigh and Atalanta run standalone take under
  1.5 s in total (`logs/quaigh_atalanta_results.md`). They grade
  combinational netlists only; neither inserts scan.
- **Raw fault coverage:** FaultFlow's is higher than Fault PRNG's on 9
  designs, equal on 3 and lower on 4. Fault produced fewer vectors (404 vs
  998).
- **c2670:** Fault's first PRNG run took 4648.3 s (`logs/fault_sweep.log`).
  It overlapped a C++ build on the same machine, so it was rerun alone, and
  the rerun took 733.4 s (`logs/fault_c2670_rerun.log`). The table uses the
  rerun. With the first run, the ratio of totals would be 37.9x.
- **Earlier FaultFlow sweep:** a sweep with 10,000 random vectors and static
  compaction totalled 455.2 s (`logs/ff_sweep_w1.log`), 11.3x against Fault
  PRNG.
