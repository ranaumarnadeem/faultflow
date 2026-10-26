# Benchmarking

## SA Faults

Every row except those marked ¹ was run on main at 182193f (the scan
Q-branch over-credit fix), Sky130 HD, `collapsing = false`, one worker, one
end-to-end run per design; Wall covers init, scan insertion, scan check and ATPG.
Every fault a scan campaign credited was replayed with the campaign's exported
patterns through the full scan-protocol fault simulator on the scanned netlist
(`tests/python/scan_credit.py`): none failed to reproduce, in any table below.
s5378 and s15850 are not listed: scan insertion refuses the flip-flop without a
data connection that Yosys emits in each (as in s13207).

¹ Earlier run, before the over-credit fix; not repeated. A boxcar stuck-at rerun
on main at fd68085, graded the same way, reproduced every credit. picorv32a's
stuck-at Hybrid row was rerun at 182193f like the others; only its Random and
transition rows are earlier runs.

### Hybrid ATPG

Random vectors to 85% coverage, then SAT.

| Design | Cells | FFs | Chains | Coverage | Redundant | Vectors | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| picorv32a | 10,989 | 1,613 | 162 | 99.992% | 930 | 2,434 | 2,444 s |
| boxcar¹ | 1,243 | 1,130 | 113 | 100.00% | 0 | 1,100 | 1,243.3 s |
| genericfir_small | 3,982 | 471 | 48 | 100.00% | 17 | 406 | 107 s |
| s9234_1 | 715 | 135 | 14 | 100.00% | 36 | 226 | 39 s |
| s1238 | 320 | 18 | 2 | 100.00% | 42 | 188 | 20 s |
| s1196 | 309 | 18 | 2 | 100.00% | 20 | 161 | 17 s |
| s1488 | 319 | 6 | 1 | 100.00% | 2 | 114 | 17 s |
| s1423 | 435 | 74 | 8 | 100.00% | 2 | 102 | 16 s |
| s838_1 | 207 | 32 | 4 | 100.00% | 0 | 134 | 15 s |
| s1494 | 332 | 6 | 1 | 100.00% | 5 | 114 | 15 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 0 | 36 | 13 s |
| s820 | 156 | 5 | 1 | 100.00% | 0 | 90 | 13 s |
| s832 | 165 | 5 | 1 | 100.00% | 0 | 90 | 13 s |
| s386 | 87 | 6 | 1 | 100.00% | 0 | 55 | 12 s |
| s400 | 102 | 21 | 3 | 100.00% | 2 | 43 | 12 s |
| s641 | 121 | 17 | 2 | 100.00% | 0 | 59 | 12 s |
| s298 | 76 | 14 | 2 | 100.00% | 0 | 31 | 11 s |
| s344 | 97 | 15 | 2 | 100.00% | 1 | 33 | 11 s |
| s382 | 102 | 21 | 3 | 100.00% | 0 | 43 | 11 s |
| s420_1 | 105 | 16 | 2 | 100.00% | 0 | 68 | 11 s |
| s444 | 105 | 21 | 3 | 100.00% | 2 | 48 | 11 s |
| s510 | 139 | 6 | 1 | 100.00% | 0 | 70 | 11 s |
| s526 | 112 | 21 | 3 | 100.00% | 0 | 50 | 11 s |
| s713 | 109 | 17 | 2 | 100.00% | 2 | 56 | 11 s |
| s349 | 97 | 15 | 2 | 100.00% | 1 | 33 | 10 s |
| s27 | 12 | 3 | 1 | 100.00% | 0 | 8 | 9 s |
| iiravg | 157 | 16 | 2 | 100.00% | 30 | 37 | 9 s |
| c432 | 65 | — | — | 100.00% | 4 | 49 | 6 s |
| c17 | 3 | — | — | 100.00% | 0 | 7 | 5 s |
| c499 | 160 | — | — | 100.00% | 0 | 63 | 5 s |

### Random ATPG

10,000 random vectors, no SAT, so no redundant-fault count.

| Design | Cells | FFs | Chains | Coverage | Wall |
|---|---:|---:|---:|---:|---:|
| boxcar¹ | 1,243 | 1,130 | 113 | 98.926% | 336.1 s |
| picorv32a¹ | 10,989 | 1,613 | 162 | 99.785% | 4,387.8 s |
| genericfir_small | 3,982 | 471 | 48 | 99.93% | 9,477 s |
| s9234_1 | 715 | 135 | 14 | 91.88% | 987 s |
| s1423 | 435 | 74 | 8 | 98.93% | 625 s |
| s1196 | 309 | 18 | 2 | 96.14% | 500 s |
| s1238 | 320 | 18 | 2 | 94.89% | 497 s |
| s1494 | 332 | 6 | 1 | 99.25% | 487 s |
| s641 | 121 | 17 | 2 | 98.47% | 476 s |
| s1488 | 319 | 6 | 1 | 99.25% | 467 s |
| s713 | 109 | 17 | 2 | 98.14% | 453 s |
| iiravg | 157 | 16 | 2 | 97.12% | 428 s |
| s838_1 | 207 | 32 | 4 | 59.63% | 420 s |
| s832 | 165 | 5 | 1 | 97.40% | 398 s |
| s400 | 102 | 21 | 3 | 99.59% | 390 s |
| s820 | 156 | 5 | 1 | 97.27% | 384 s |
| s444 | 105 | 21 | 3 | 99.59% | 381 s |
| s344 | 97 | 15 | 2 | 99.76% | 380 s |
| s349 | 97 | 15 | 2 | 99.76% | 378 s |
| s420_1 | 105 | 16 | 2 | 80.45% | 369 s |
| s386 | 87 | 6 | 1 | 100.00% | 221 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 210 s |
| c432 | 65 | — | — | 99.30% | 187 s |
| s526 | 112 | 21 | 3 | 100.00% | 122 s |
| s510 | 139 | 6 | 1 | 100.00% | 83 s |
| s382 | 102 | 21 | 3 | 100.00% | 44 s |
| c499 | 160 | — | — | 100.00% | 27 s |
| s298 | 76 | 14 | 2 | 100.00% | 15 s |
| s27 | 12 | 3 | 1 | 100.00% | 12 s |
| c17 | 3 | — | — | 100.00% | 6 s |

## Transition Delay Faults

### Hybrid ATPG

Launch-on-capture. Fault cov. counts SAT-proven-redundant faults as undetected.

| Design | Cells | FFs | Chains | Coverage | Fault cov. | Redundant | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| boxcar¹ | 1,243 | 1,130 | 113 | 100.00% | 80.98% | 5,326 | ~11.7 h |
| picorv32a¹ ² | 10,989 | 1,613 | 162 | 94.73% | 89.81% | 3,861 | ~10.5 h |
| genericfir_small | 3,982 | 471 | 48 | 99.80% | 85.41% | 3,722 | 985 s |
| s9234_1 | 715 | 135 | 14 | 99.65% | 86.84% | 423 | 74 s |
| s1238 | 320 | 18 | 2 | 99.74% | 18.68% | 1,671 | 36 s |
| s1423 | 435 | 74 | 8 | 99.75% | 76.02% | 490 | 28 s |
| s1488 | 319 | 6 | 1 | 100.00% | 82.00% | 382 | 28 s |
| s1196 | 309 | 18 | 2 | 99.74% | 19.98% | 1,513 | 26 s |
| s1494 | 332 | 6 | 1 | 100.00% | 79.92% | 427 | 23 s |
| s820 | 156 | 5 | 1 | 100.00% | 57.68% | 419 | 20 s |
| s713 | 109 | 17 | 2 | 99.75% | 56.14% | 306 | 19 s |
| s641 | 121 | 17 | 2 | 99.76% | 57.64% | 304 | 17 s |
| s838_1 | 207 | 32 | 4 | 100.00% | 76.15% | 218 | 17 s |
| s832 | 165 | 5 | 1 | 100.00% | 59.33% | 423 | 16 s |
| s420_1 | 105 | 16 | 2 | 100.00% | 75.00% | 110 | 15 s |
| s510 | 139 | 6 | 1 | 100.00% | 75.06% | 210 | 15 s |
| iiravg | 157 | 16 | 2 | 100.00% | 75.38% | 256 | 15 s |
| s349 | 97 | 15 | 2 | 100.00% | 87.08% | 54 | 14 s |
| s386 | 87 | 6 | 1 | 100.00% | 55.94% | 215 | 14 s |
| s400 | 102 | 21 | 3 | 100.00% | 76.34% | 115 | 14 s |
| s444 | 105 | 21 | 3 | 100.00% | 75.73% | 117 | 13 s |
| s382 | 102 | 21 | 3 | 100.00% | 75.93% | 116 | 12 s |
| s526 | 112 | 21 | 3 | 100.00% | 61.97% | 197 | 12 s |
| s208_1 | 45 | 8 | 1 | 100.00% | 71.57% | 58 | 10 s |
| s298 | 76 | 14 | 2 | 100.00% | 78.11% | 74 | 10 s |
| c499 | 160 | — | — | 100.00% | 100.00% | 0 | 9 s |
| s27 | 12 | 3 | 1 | 91.30% | 35.00% | 37 | 9 s |
| s344 | 97 | 15 | 2 | 100.00% | 87.08% | 54 | 9 s |
| c17 | 3 | — | — | 100.00% | 100.00% | 0 | 6 s |
| c432 | 65 | — | — | 100.00% | 99.30% | 4 | 6 s |

² Stopped after two ATPG rounds, before compaction.

genericfir_small ended at `MAX_ROUNDS` with 44 faults undetected. A Random
transition table was published here before; its runs predate the over-credit fix
and were not repeated, so it is withdrawn (see git history).

## Hierarchical Testing

| SoC | Chip denominator | Chip detected | Chip coverage | Guards |
|---|---:|---:|---:|---|
| SoC1 (picorv32a+uart) | 70,713 | 70,507 | 99.71% | 4/4 pass |
| SoC2 (serv+uart) | 8,768 | 8,732 | 99.59% | 4/4 pass |
| SoC3 (tiny_aes+uart+controller) | 191,263 | 191,203 | 99.97% | 4/4 pass |
| SoC4 (iiravg+boxcar+genericfir_small) | 40,643 | 40,643 | 100.00% | 4/4 pass |

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

| Block | SoC context | Chain length | Patterns verified |
|---|---|---:|---:|
| iiravg | SoC4 | 16 (2 chains) | 78/78 |
| boxcar | SoC4 | 1,130 (113 chains) | 30/30 |
| genericfir_small | SoC4 | 471 (48 chains) | 30/30 |
| picorv32a | SoC1 | 1,613 (162 chains) | 2/2 |
| uart | SoC1 | 131 (2 chains) | 5/5 |
| uart | SoC2 | 131 (2 chains) | 5/5 |
| uart | SoC3 | 131 (2 chains) | 5/5 |
| serv | SoC2 | 164 (20 chains) | 10/10 |
| soc3_controller | SoC3 | 411 (45 chains) | 5/5 |
| tiny_aes | SoC3 | 5,568 (600 chains) | 1/1 |
