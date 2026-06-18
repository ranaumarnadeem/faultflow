# Fault 0.9.4 — Benchmark Results

**Tool:** [Fault](https://github.com/AUCOHL/Fault) v0.9.4  
**Library:** OSU035 (`osu035_stdcells.lib`)  
**Flow:** `fault synth` → `fault cut` → `fault` (PRNG, -v 100 -r 50 -m 95 --ceiling 200)  
**Host:** Ubuntu (WSL2, Windows 11)  
**Date:** 2026-06-18

## ISCAS-85 (Combinational)

Pure combinational logic; a `dummy_clk` port is injected into the RTL before synthesis
as required by Fault's scan-insertion model.

| Design | Gates | Fault Sites | Coverage | Compact TVs | Synth | Sim |
|--------|------:|------------:|---------:|------------:|------:|----:|
| c17 | 6 | 25 | 100.00% | 4 | 31s | 36s |
| c432 | 101 | 388 | 98.71% | 18 | 30s | 44s |
| c499 | 174 | 603 | 96.10% | 16 | 30s | 48s |

## ISCAS-89 (Sequential)

Sequential circuits; FFs removed by `fault cut --clock blif_clk_net --reset blif_reset_net`.
Coverage is over the resulting combinational cut netlist.

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

## Notes

- **Gates**: post-synthesis gate count from `yosys stat` (OSU035 standard cells only; sequential cells excluded from ABC combinational mapping).
- **Fault Sites**: stuck-at-0/stuck-at-1 sites found in the cut (combinational) netlist by Fault's enumerator.
- **Coverage**: percentage of fault sites detected by the final compact test-vector set.
- **Compact TVs**: vector count after Fault's internal compaction pass.
- **Synth**: wall-clock time for `fault synth` (Yosys synthesis).
- **Sim**: wall-clock time for fault simulation (PRNG, ceiling 200 vectors).
- Designs that hit the 200-vector ceiling without reaching 95% target report coverage as achieved at ceiling.
- The `dummy_clk` port added to ISCAS-85 RTL is not connected to any logic and does not affect results.
- ISCAS-89 coverage is over the **cut** (combinational) netlist — sequential fault coverage (flip-flop state faults) is not reported by Fault's default PRNG mode.
