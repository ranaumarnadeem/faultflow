# Multicore scaling on picorv32a

How FaultFlow's run time changes with its two kinds of parallelism, on one
design large enough for it to matter: picorv32a (Sky130 HD), stuck-at, scan
ATPG with 162 chains, Hybrid (random vectors, then SAT).

- **`[atpg] workers`:** SAT ATPG processes, each solving its own faults.
- **`[simulation] sim_threads`:** threads that split the bit-parallel fault
  simulation's fault batches.

On small designs neither helps much: a profile of genericfir_small found the
bit-parallel simulation that `sim_threads` splits to be about 11% of the scan
simulation step, the rest per-candidate database commits, the scalar golden
check and netlist re-reads.

## The harness

`run.sh` runs these (workers, sim_threads) pairs in order, one at a time:
(1,1), (1,2), (1,4), (1,8), (2,1), (4,1), (8,1), (8,8), and (1,1) again last.
The repeat of the baseline shows the noise of one sample. Each run:

- waits until the 1-minute load has been under 1.0 for 5 minutes;
- times setup (`ff.py init`, `scan --scan-chains 162 --techmap`, `scan-check`)
  and `ff.py sim --scan --export-patterns` separately;
- samples the load every minute and records the highest, and the longest gap
  between samples (a gap well over 60 s means the machine slept);
- appends one `RESULT` line to `summary.txt`: times, vector count, coverage,
  detected, redundant, SAT timeouts, terminal reason, ATPG and fault-simulation
  seconds.

A stopped sweep resumes from `summary.txt` when run again. `RUNS` narrows it,
and `SKIP_IDLE=1` starts each run at once, for a smoke test of the script only:
its times are not results. `machine.txt` records the commit, kernel, CPU and
memory.

To run it, build the FaultFlow commit under test in an extracted archive and
write its full SHA to `.commit` there:

```bash
M=/path/to/archive VENV=/path/to/venv NETLIST=/path/to/picorv32a_sky130.json OUT=/path/to/out bash docs/benchmarks/scaling/run.sh
```

Expect about 6 hours on an idle machine with 10 logical CPUs: each run takes
30 to 45 minutes.

## Earlier measurement

One sweep on main as of 2026-10-25, with workers and sim_threads raised
together. Its raw logs were deleted with the benchmark scratch area on
2026-11-11; the figures are from the notes kept then:

| workers = sim_threads | Total | Vectors | SAT timeouts | Coverage |
|---:|---:|---:|---:|---:|
| 1 | 2444 s | 2434 | 24 | 99.992% |
| 2 | 2119 s | 2434 | 24 | 99.992% |
| 4 | 2039 s | 2433 | 28 | 99.992% |
| 8 | 1990 s | 2431 | 41 | 99.992% |

All four detected 87,517 faults and proved 930 redundant. Eight of each gave
1.23x over one. SAT results are taken in a fixed fault order, so only the
per-fault SAT timeout can make the workers differ: it is wall-clock (10 s by
default), more workers share the CPUs, and a fault that times out changes the
vectors after it. Hence the timeouts rising with the worker count and the
vector count moving by up to 3. Fault simulation is bit-identical across
`sim_threads` (tested). One sample per point. (These runs predate the
conflict-cap default, `[atpg] sat_conflict_schedule`: with it, picorv32a
stuck-at gives identical results at 1 and 4 workers and under load.)

## Status

The sweep above has not been run yet: it needs an idle machine.
