# Benchmarking

## SA Faults

### Hybrid ATPG

| Design | Cells | FFs | Chains | Coverage | Redundant | Vectors | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| s15850 | 2,644 | 559 | 56 | 100.00% | 223 | 866 | 2,654.3 s |
| s5378 | 845 | 162 | 17 | 100.00% | 18 | 577 | 994.2 s |
| s9234_1 | 715 | 135 | 14 | 100.00% | 59 | 505 | 769.3 s |
| s1423 | 435 | 74 | 8 | 100.00% | 19 | 301 | 192.5 s |
| s1494 | 332 | 6 | 1 | 100.00% | 7 | 370 | 177.9 s |
| s1238 | 320 | 18 | 2 | 100.00% | 52 | 448 | 194.7 s |
| s1488 | 319 | 6 | 1 | 100.00% | 2 | 369 | 178.9 s |
| s1196 | 309 | 18 | 2 | 100.00% | 20 | 426 | 163.8 s |
| s838_1 | 207 | 32 | 4 | 100.00% | 6 | 435 | 179.6 s |
| s832 | 165 | 5 | 1 | 100.00% | 9 | 361 | 112.4 s |
| s820 | 156 | 5 | 1 | 100.00% | 0 | 362 | 115.2 s |
| s510 | 139 | 6 | 1 | 100.00% | 0 | 227 | 73.7 s |
| s641 | 121 | 17 | 2 | 100.00% | 0 | 325 | 108.7 s |
| s526n | 113 | 21 | 3 | 100.00% | 1 | 300 | 87.8 s |
| s526 | 112 | 21 | 3 | 100.00% | 3 | 283 | 87.6 s |
| s713 | 109 | 17 | 2 | 100.00% | 3 | 320 | 107.0 s |
| s444 | 105 | 21 | 3 | 100.00% | 8 | 173 | 46.5 s |
| s420_1 | 105 | 16 | 2 | 100.00% | 2 | 363 | 117.7 s |
| s400 | 102 | 21 | 3 | 100.00% | 7 | 176 | 52.5 s |
| s382 | 102 | 21 | 3 | 100.00% | 3 | 125 | 32.6 s |
| s349 | 97 | 15 | 2 | 100.00% | 1 | 97 | 24.3 s |
| s344 | 97 | 15 | 2 | 100.00% | 1 | 97 | 24.1 s |
| s386 | 87 | 6 | 1 | 100.00% | 0 | 324 | 85.9 s |
| s298 | 76 | 14 | 2 | 100.00% | 1 | 60 | 15.2 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 2 | 321 | 81.3 s |
| s27 | 12 | 3 | 1 | 100.00% | 0 | 37 | 8.5 s |
| c499 | 160 | — | — | 100.00% | 0 | 172 | 30.4 s |
| c432 | 65 | — | — | 100.00% | 4 | 99 | 19.1 s |
| c17 | 3 | — | — | 100.00% | 0 | 13 | 3.0 s |
| iiravg | — | 16 | 2 | 100.00% | 30 | 36 | 16.7 s |
| boxcar | — | 1,130 | 113 | 100.00% | 0 | 1,100 | 1,243.3 s |
| genericfir_small | — | 471 | 48 | 100.00% | 17 | 344 | 338.7 s |
| picorv32a | — | — | 162 | 99.976% | 918 | 1,784 | 13,913.8 s |

### Hybrid ATPG — Re-run (current build, `collapsing = false`, same methodology as above)

Re-run of the same corpus against the current build. `s5378`/`s15850` were not re-run this
pass (Yosys sliced some flip-flops into `$auto$ff.cc:337:slice$...` cells that don't fit
faultflow's scan-insertion shape requirements on this build — a genuine per-design gap, not a
methodology change). Coverage is 100.000% on every design except `picorv32a`; both figures are
within noise of the original 99.976%.

| Design | Coverage | Vectors | Wall |
|---|---:|---:|---:|
| s9234_1 | 100.00% | 154 | 203.7 s |
| picorv32a | 99.956% | 2,160 | 9,971.6 s |
| s1423 | 100.00% | 74 | 61.1 s |
| s1494 | 100.00% | 98 | 87.0 s |
| s1238 | 100.00% | 74 | 33.6 s |
| s1488 | 100.00% | 25 | 9.8 s |
| s1196 | 100.00% | 161 | 117.1 s |
| s838_1 | 100.00% | 91 | 57.1 s |
| s832 | 100.00% | 80 | 54.3 s |
| s820 | 100.00% | 81 | 51.0 s |
| s510 | 100.00% | 57 | 32.3 s |
| s641 | 100.00% | 57 | 35.6 s |
| s526n | 100.00% | 30 | 20.6 s |
| s526 | 100.00% | 31 | 19.8 s |
| s713 | 100.00% | 54 | 35.4 s |
| s444 | 100.00% | 22 | 16.9 s |
| s420_1 | 100.00% | 46 | 30.2 s |
| s400 | 100.00% | 24 | 17.0 s |
| s382 | 100.00% | 29 | 17.2 s |
| s349 | 100.00% | 30 | 21.5 s |
| s344 | 100.00% | 30 | 21.6 s |
| s386 | 100.00% | 40 | 26.7 s |
| s298 | 100.00% | 25 | 10.4 s |
| s208_1 | 100.00% | 26 | 20.0 s |
| s27 | 100.00% | 9 | 4.9 s |
| c499 | 100.00% | 63 | 8.8 s |
| c432 | 100.00% | 49 | 6.0 s |
| c17 | 100.00% | 7 | 0.9 s |
| iiravg | 100.00% | 37 | 32.3 s |
| boxcar | 100.00% | 2,151 | 4,087.0 s |
| genericfir_small | 100.00% | 404 | 1,606.5 s |

Most designs converge in noticeably fewer vectors and less wall-clock than the original table
(e.g. `s1488` 369→25 vectors, 18x faster; `s1238` 448→74 vectors, 5.8x faster) — plausible given
ATPG/compaction improvements landed on this build since the original run, but not verified
against a specific changelog entry. Three designs went the other way (`boxcar`, `genericfir_small`,
`iiravg` all got slower with more vectors, not fewer) — worth a closer look if a performance
regression matters here; not investigated further in this pass.

### Hybrid ATPG — Re-run (current build, `collapsing = true`)

Same corpus and current build as the re-run above, with fault collapsing enabled this time
(primitive-gate equivalence plus the exhaustively-derived Sky130 compound-cell classes — see
`docs/collapsing_rules.md`). `s5378`/`s15850` excluded for the same reason as above. Coverage is
100.000% everywhere except `picorv32a` (99.969%, consistent with the other two runs). The
`Collapsed / Raw faults` column makes the effect concrete: collapsing is folding a real, sizable
fraction of the enumerated fault set away before ATPG ever runs on every design, not just a
handful of trivial cases.

| Design | Coverage | Vectors | Wall | Collapsed / Raw faults |
|---|---:|---:|---:|---:|
| s9234_1 | 100.00% | 146 | 128.4 s | 642 / 4,638 |
| picorv32a | 99.969% | 2,441 | 13,044.3 s | 21,461 / 97,346 |
| s1423 | 100.00% | 71 | 40.8 s | 503 / 2,802 |
| s1494 | 100.00% | 96 | 64.1 s | 522 / 2,192 |
| s1238 | 100.00% | 187 | 93.1 s | 483 / 2,240 |
| s1488 | 100.00% | 104 | 50.8 s | 471 / 2,188 |
| s1196 | 100.00% | 161 | 82.1 s | 468 / 2,076 |
| s838_1 | 100.00% | 81 | 39.7 s | 227 / 1,240 |
| s832 | 100.00% | 80 | 39.1 s | 240 / 1,096 |
| s820 | 100.00% | 81 | 37.9 s | 250 / 1,046 |
| s510 | 100.00% | 56 | 22.7 s | 186 / 908 |
| s641 | 100.00% | 57 | 26.4 s | 186 / 896 |
| s526n | 100.00% | 29 | 14.5 s | 98 / 746 |
| s526 | 100.00% | 28 | 14.5 s | 101 / 734 |
| s713 | 100.00% | 54 | 27.6 s | 170 / 876 |
| s444 | 100.00% | 23 | 17.8 s | 93 / 692 |
| s420_1 | 100.00% | 40 | 25.6 s | 101 / 606 |
| s400 | 100.00% | 24 | 15.2 s | 99 / 696 |
| s382 | 100.00% | 29 | 11.6 s | 94 / 692 |
| s349 | 100.00% | 28 | 13.9 s | 100 / 574 |
| s344 | 100.00% | 28 | 13.7 s | 100 / 574 |
| s386 | 100.00% | 40 | 17.2 s | 94 / 554 |
| s298 | 100.00% | 24 | 6.2 s | 60 / 484 |
| s208_1 | 100.00% | 22 | 15.9 s | 32 / 290 |
| s27 | 100.00% | 9 | 3.8 s | 10 / 96 |
| c499 | 100.00% | 61 | 12.6 s | 164 / 1,022 |
| c432 | 100.00% | 47 | 10.2 s | 64 / 574 |
| c17 | 100.00% | 7 | 1.3 s | 6 / 28 |
| iiravg | 100.00% | 37 | 31.5 s | 150 / 1,174 |
| boxcar | 100.00% | 2,150 | 3,777.7 s | 9,932 / 34,842 |
| genericfir_small | 100.00% | 413 | 1,536.1 s | 4,484 / 29,458 |

### Random ATPG

Random ATPG never invokes SAT, so it has no meaningful redundant-fault count to
report (the `Redundant` column is omitted here for that reason).

| Design | Cells | FFs | Chains | Coverage | Vectors | Wall |
|---|---:|---:|---:|---:|---:|---:|
| c17 | 3 | — | — | 100.00% | — | 10.5 s |
| c432 | 65 | — | — | 100.00% | — | 11.9 s |
| c499 | 160 | — | — | 100.00% | — | 17.1 s |
| s27 | 12 | 3 | 1 | 100.00% | — | 12.0 s |
| s208_1 | 45 | 8 | 1 | 100.00% | — | 20.0 s |
| s298 | 76 | 14 | 2 | 99.36% | — | 13.2 s |
| s344 | 97 | 15 | 2 | 100.00% | — | 17.2 s |
| s349 | 97 | 15 | 2 | 100.00% | — | 16.6 s |
| s382 | 102 | 21 | 3 | 100.00% | — | 22.4 s |
| s386 | 87 | 6 | 1 | 99.05% | — | 30.2 s |
| s400 | 102 | 21 | 3 | 99.59% | — | 25.7 s |
| s420_1 | 105 | 16 | 2 | 97.25% | — | 22.8 s |
| s444 | 105 | 21 | 3 | 98.92% | — | 21.2 s |
| s510 | 139 | 6 | 1 | 98.91% | — | 24.8 s |
| s526 | 112 | 21 | 3 | 98.66% | — | 24.9 s |
| s526n | 113 | 21 | 3 | 99.22% | — | 22.2 s |
| s641 | 121 | 17 | 2 | 99.47% | — | 22.6 s |
| s713 | 109 | 17 | 2 | 100.00% | — | 19.4 s |
| s820 | 156 | 5 | 1 | 98.99% | — | 33.8 s |
| s832 | 165 | 5 | 1 | 97.98% | — | 31.4 s |
| s838_1 | 207 | 32 | 4 | 97.66% | — | 29.5 s |
| s1196 | 309 | 18 | 2 | 99.33% | — | 50.7 s |
| s1238 | 320 | 18 | 2 | 97.42% | — | 63.7 s |
| s1423 | 435 | 74 | 8 | 99.68% | — | 40.7 s |
| s1488 | 319 | 6 | 1 | 99.49% | — | 39.7 s |
| s1494 | 332 | 6 | 1 | 99.57% | — | 38.4 s |
| s5378 | 845 | 162 | 17 | 99.48% | — | 141.9 s |
| s9234_1 | 715 | 135 | 14 | 99.17% | — | 106.8 s |
| s15850 | 2,644 | 559 | 56 | 99.16% | — | 690.9 s |
| iiravg | — | 16 | 2 | 100.00% | 30 | 7.0 s |
| boxcar | — | 1,130 | 113 | 98.926% | 288 | 336.1 s |
| genericfir_small | — | 471 | 48 | 99.981% | 287 | 590.0 s |
| picorv32a | — | — | 162 | 99.785% | 1,164 | 4,387.8 s |

## Transition Delay Faults

### Hybrid ATPG

| Design | Cells | FFs | Chains | Coverage | Fault cov. | Redundant | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| s15850 | 2,644 | 559 | 56 | 99.05% | 65.41% | 4,837 | 1.80 h |
| s5378 | 845 | 162 | 17 | 100.00% | 71.52% | 1,323 | 995.7 s |
| s9234_1 | 715 | 135 | 14 | 99.91% | 86.06% | 549 | 834.4 s |
| s1423 | 435 | 74 | 8 | 100.00% | 74.82% | 613 | 440.6 s |
| s1494 | 332 | 6 | 1 | 100.00% | 81.24% | 416 | 179.9 s |
| s1238 | 320 | 18 | 2 | 100.00% | 20.19% | 1,668 | 197.7 s |
| s1488 | 319 | 6 | 1 | 100.00% | 80.78% | 412 | 176.3 s |
| s1196 | 309 | 18 | 2 | 100.00% | 20.41% | 1,560 | 231.9 s |
| s838_1 | 207 | 32 | 4 | 100.00% | 74.83% | 289 | 230.4 s |
| s832 | 165 | 5 | 1 | 100.00% | 60.15% | 432 | 135.6 s |
| s820 | 156 | 5 | 1 | 100.00% | 58.72% | 440 | 143.2 s |
| s510 | 139 | 6 | 1 | 100.00% | 76.00% | 216 | 112.1 s |
| s641 | 121 | 17 | 2 | 99.75% | 53.49% | 346 | 141.5 s |
| s526n | 113 | 21 | 3 | 100.00% | 61.13% | 248 | 116.2 s |
| s526 | 112 | 21 | 3 | 100.00% | 61.76% | 244 | 119.1 s |
| s713 | 109 | 17 | 2 | 99.74% | 53.38% | 330 | 197.7 s |
| s444 | 105 | 21 | 3 | 100.00% | 67.87% | 196 | 160.5 s |
| s420_1 | 105 | 16 | 2 | 100.00% | 73.84% | 147 | 150.8 s |
| s400 | 102 | 21 | 3 | 100.00% | 67.61% | 195 | 126.5 s |
| s382 | 102 | 21 | 3 | 100.00% | 68.05% | 193 | 126.1 s |
| s349 | 97 | 15 | 2 | 100.00% | 85.26% | 74 | 104.8 s |
| s344 | 97 | 15 | 2 | 100.00% | 85.26% | 74 | 103.9 s |
| s386 | 87 | 6 | 1 | 100.00% | 58.02% | 225 | 140.2 s |
| s298 | 76 | 14 | 2 | 100.00% | 74.28% | 107 | 146.2 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 73.97% | 63 | 104.2 s |
| s27 | 12 | 3 | 1 | 100.00% | 37.50% | 40 | 44.7 s |
| c499 | 160 | — | — | 100.00% | 100.00% | 0 | 64.3 s |
| c432 | 65 | — | — | 100.00% | 99.30% | 4 | 52.6 s |
| c17 | 3 | — | — | 100.00% | 100.00% | 0 | 26.3 s |
| iiravg | — | 16 | 2 | 100.00% | — | 256 | 774.2 s |
| boxcar¹ | — | 1,130 | 113 | 100.00% | 80.98% | 5,326 | ~11.7 h |
| genericfir_small | — | 471 | 48 | 100.00% | — | 3,682 | 4,012.4 s |
| picorv32a | — | — | 162 | 94.73% | 89.81% | 3,861 | ~10.5 h |

¹ Restarted mid-run after diagnosing a WSL 9P-bridge I/O bottleneck (the SQLite DB, grown
large, was living on `/mnt/c`; relocated to native ext4 storage via symlink, preserving all
prior progress with no data loss). `atpg_terminal=COMPLETE`, 0 timeouts, 0 unknowns —
fully converged. Killed intentionally after all 27,988 faults resolved (0 undetected), once
a subsequent vector-compaction phase began growing the DB unboundedly (29 GB in <1 h) with
no further effect on the coverage numerator/denominator.

### Random ATPG

Random ATPG never invokes SAT, so it has no meaningful redundant-fault count to
report (the `Redundant` column is omitted here for that reason; most rows below
had inherited their Hybrid row's Redundant count before this column was dropped,
which was never itself a real random-campaign measurement).

| Design | Cells | FFs | Chains | Coverage | Fault cov. | Wall |
|---|---:|---:|---:|---:|---:|---:|
| s15850 | 2,644 | 559 | 56 | 99.05% | 65.41% | 1.43 h |
| s5378 | 845 | 162 | 17 | 100.00% | 71.52% | 954.1 s |
| s9234_1 | 715 | 135 | 14 | 99.91% | 86.06% | 604.4 s |
| s1423 | 435 | 74 | 8 | 100.00% | 74.82% | 181.4 s |
| s1494 | 332 | 6 | 1 | 100.00% | 81.24% | 117.7 s |
| s1238 | 320 | 18 | 2 | 100.00% | 20.19% | 114.8 s |
| s1488 | 319 | 6 | 1 | 100.00% | 80.78% | 104.2 s |
| s1196 | 309 | 18 | 2 | 100.00% | 20.41% | 103.5 s |
| s838_1 | 207 | 32 | 4 | 100.00% | 74.83% | 91.9 s |
| s832 | 165 | 5 | 1 | 100.00% | 60.15% | 57.5 s |
| s820 | 156 | 5 | 1 | 100.00% | 58.72% | 61.6 s |
| s510 | 139 | 6 | 1 | 100.00% | 76.00% | 40.9 s |
| s641 | 121 | 17 | 2 | 99.75% | 53.49% | 53.0 s |
| s526n | 113 | 21 | 3 | 100.00% | 61.13% | 38.6 s |
| s526 | 112 | 21 | 3 | 100.00% | 61.76% | 38.7 s |
| s713 | 109 | 17 | 2 | 99.74% | 53.38% | 51.0 s |
| s444 | 105 | 21 | 3 | 100.00% | 67.87% | 37.9 s |
| s420_1 | 105 | 16 | 2 | 100.00% | 73.84% | 44.1 s |
| s400 | 102 | 21 | 3 | 100.00% | 67.61% | 39.1 s |
| s382 | 102 | 21 | 3 | 100.00% | 68.05% | 37.9 s |
| s349 | 97 | 15 | 2 | 100.00% | 85.26% | 31.1 s |
| s344 | 97 | 15 | 2 | 100.00% | 85.26% | 31.2 s |
| s386 | 87 | 6 | 1 | 100.00% | 58.02% | 34.5 s |
| s298 | 76 | 14 | 2 | 100.00% | 74.28% | 26.0 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 73.97% | 22.9 s |
| s27 | 12 | 3 | 1 | 100.00% | 37.50% | 14.2 s |
| c499 | 160 | — | — | 100.00% | 100.00% | 41.8 s |
| c432 | 65 | — | — | 100.00% | 99.30% | 22.0 s |
| c17 | 3 | — | — | 100.00% | 100.00% | 5.6 s |
| iiravg | — | 16 | 2 | 100.00% | 75.38% | 57.8 s |
| boxcar² | 1,243 | 1,130 | 113 | 74.696% | 74.696% | 4.887 h |
| genericfir_small | — | 471 | 48 | 100.00% | 85.73% | 2.49 h |
| picorv32a | — | — | 162 | 98.89% | 93.22% | ~7 h |

² Run 2026-08-16/17 as a genuinely fresh, isolated `random_only=true` campaign
(separate output directory, no shared DB with the Hybrid run above) — never invokes
SAT, so Coverage == Fault cov. (no SAT-proven-redundant faults to exclude). 1 round,
5,000 candidate vectors generated / 2,589 accepted (2,411 rejected as non-detecting).
Run in two phases (an initial `sim_threads=4` phase to 390 accepted vectors, then
restarted with `sim_threads=8` for the remainder — `sim_threads` is not part of the
resume fingerprint, so this did not require a fresh campaign); Wall is the sum of
each phase's active compute time (~1.0 h + 3.887 h), excluding an intervening pause.
This is a different provenance than most other rows in this table -- see the note
above the table.

## Hierarchical Testing

| SoC | Chip denominator | Chip detected | Chip coverage | Guards |
|---|---:|---:|---:|---|
| SoC1 (picorv32a+uart) | 70,713 | 70,507 | 99.71% | 4/4 pass |
| SoC2 (serv+uart) | 8,768 | 8,732 | 99.59% | 4/4 pass |
| SoC3 (tiny_aes+uart+controller) | 191,263 | 191,203 | 99.97% | 4/4 pass |
| SoC4 (iiravg+boxcar+genericfir_small) | 40,643 | 40,643 | 100.00% | 4/4 pass |

### SoC1 (picorv32a + uart)

| Scope | Kind | Denominator | Detected | Coverage |
|---|---|---:|---:|---:|
| picorv32a | block (INTEST) | 65,986 | 65,945 | 99.94% |
| uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| soc1_extest | interconnect (EXTEST) | 953 | 788 | 82.69% |

| Block | Chain length | Patterns verified |
|---|---:|---:|
| picorv32a | 1,613 (162 chains) | 2/2 |
| uart | 131 (2 chains) | 5/5 |

### SoC2 (serv + uart)

| Scope | Kind | Denominator | Detected | Coverage |
|---|---|---:|---:|---:|
| serv | block (INTEST) | 4,217 | 4,217 | 100.00% |
| uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| soc2_extest | interconnect (EXTEST) | 777 | 741 | 95.37% |

| Block | Chain length | Patterns verified |
|---|---:|---:|
| serv | 164 (20 chains) | 10/10 |
| uart | 131 (2 chains) | 5/5 |

### SoC3 (tiny_aes + uart + controller)

| Scope | Kind | Denominator | Detected | Coverage |
|---|---|---:|---:|---:|
| tiny_aes | block (INTEST) | 179,354 | 179,354 | 100.00% |
| uart | block (INTEST) | 3,774 | 3,774 | 100.00% |
| controller | block (INTEST) | 7,157 | 7,157 | 100.00% |
| soc3_extest | interconnect (EXTEST) | 978 | 918 | 93.87% |

| Block | Chain length | Patterns verified |
|---|---:|---:|
| tiny_aes | 5,568 (600 chains) | 1/1 |
| uart | 131 (2 chains) | 5/5 |
| soc3_controller | 411 (45 chains) | 5/5 |

### SoC4 (iiravg + boxcar + genericfir_small)

| Scope | Kind | Denominator | Detected | Coverage |
|---|---|---:|---:|---:|
| iiravg | block (INTEST) | 924 | 924 | 100.00% |
| boxcar | block (INTEST) | 18,148 | 18,148 | 100.00% |
| genericfir_small | block (INTEST) | 21,409 | 21,409 | 100.00% |
| soc4_extest | interconnect (EXTEST) | 162 | 162 | 100.00% |

| Block | Chain length | Patterns verified |
|---|---:|---:|
| iiravg | 16 (2 chains) | 78/78 |
| boxcar | 1,130 (113 chains) | 30/30 |
| genericfir_small | 471 (48 chains) | 30/30 |
