# Outputs

Every campaign writes into a per-design workspace, `output/<top>/`. The user-facing
deliverables live at the top of that directory; transient internal state lives in
`output/<top>/.faultflow/` and is what `--clean` removes.

## Directory layout

```text
output/<top>/
├── coverage.rpt           # human-readable coverage report
├── rule_check.rpt         # DFT rule-check report (rule_check command)
├── report.rpt             # unified report (shell `report` command)
├── <top>_wrapped.v        # wrap deliverable: the design in its IEEE 1500 wrapper, sky130 cells
├── <top>_wrapped.json     # the same, JSON
├── wrap.rpt               # which port bits the wrapper wraps, and why
├── <top>_scan.v           # scan deliverable (after scan + techmap)
├── <top>_scan.json        # scanned generic JSON
├── <top>_compressed.v     # scan-compress deliverable: the decompressor around the design
├── <top>_compressed.json  # the same, generic JSON
├── <top>_compacted.v      # scan-compact deliverable: the compactor around it (with both: the chip)
├── <top>_compacted.json   # the same, generic JSON
├── scan.rpt               # scan-insertion report
└── .faultflow/            # internal workspace (removed by --clean)
    ├── faultflow.sqlite    # campaign database (combinational + scan)
    ├── logs/               # Yosys / nl2bench / quaigh logs
    ├── manifests/          # scan_manifest.json
    ├── intermediate/       # <top>.json, <top>_gate.v, coverage_report.json
    ├── verification/       # iverilog verification reports
    └── generated_scripts/  # rendered yosys_synth.tcl
```

In OpenTestability oracle mode (`run`), an `oracle_response.json` is written to the
output root.

## `scan_manifest.json`

What `ff.py scan` inserted, validated against
[`schemas/scan_manifest.schema.json`](https://github.com/ranaumarnadeem/faultflow/blob/main/schemas/scan_manifest.schema.json):
the scanned netlist and its sky130 Verilog, the scan ports, every chain (its `kind`,
`core` or `wrapper`, its scan ports and its cells in shift order), every scan cell's
nets, and the flip-flops left out of scan with the reason. A design scanned with
`[wrap] enabled` also gets a `wrapper` entry
([`schemas/wrapper.schema.json`](https://github.com/ranaumarnadeem/faultflow/blob/main/schemas/wrapper.schema.json)):
its mode pins and clock, and per boundary cell its port bit, side, mux, gate and flop
instances, the nets on its system and core sides, and its flop's chain and position.
Later steps add their own entries (`latest_check`, `compression`, `compaction`).

## `coverage.rpt`

A plain-text summary of the campaign: the coverage headline, the fault classification
counts, the policy in effect, and per-node detail. The file name is set by
`[report] output`. (The interactive shell additionally writes `report.rpt`, a unified
report with a scan-focused headline; the batch CLI flow produces `coverage.rpt`.)

## `coverage_report.json`

The machine-readable report, written to
`output/<top>/.faultflow/intermediate/coverage_report.json` and validated against
[`schemas/coverage.schema.json`](https://github.com/ranaumarnadeem/faultflow/blob/main/schemas/coverage.schema.json).
Its top-level structure is:

| Section | Contents |
|---|---|
| `metadata` | Tool/run identity and input fingerprints |
| `policy` | The active policies (collapsing, clock/reset inclusion, unsupported-cell mode, how `[blackbox]` instances were modeled and, for scan, what their outputs hold) |
| `summary` | The coverage and fault-count totals (see below) |
| `run` | Per-run statistics, including ATPG round counts and timing |
| `per_node` | Per-net fault counts and detection status |
| `undetected_faults` | The remaining undetected/redundant faults |
| `autombist_categories` | Only with `[autombist] manifest`: `detected`, `denominator`, `blackbox_unresolved`, `hold_unresolved` and `coverage_percent` per autoMBIST instance category, `glue` holding what no instance owns; they add up to the `summary` totals. After `ff.py jtag`, also `combined_detected`, `combined_denominator` and `combined_coverage_percent` (scan and JTAG credit together). `coverage.rpt` shows the same as a table. See [External tools](../external_tools.md) |
| `jtag` | Only after `ff.py jtag`: the latest JTAG network-integrity grade of this campaign's faults -- the program (`program_digest`, `tck_periods`, `tests`), `graded`, `detected`, `detected_by_test` (each fault credited to the first test that detects it), `detected_only_by_jtag`, `reset_path_ungraded` (faults that could keep a flop out of reset, which a two-valued grade can't judge), `scan_redundant_conflicts` (scan-redundant faults JTAG detects; expected empty) and the `holds` used. The `summary` block is unchanged |
| `combined` | Only after `ff.py jtag`: scan and JTAG credit together -- `detected` by either, `redundant` only if scan proved it and JTAG didn't detect it, and the two coverage figures over them. The Policy-3 totals hold with these numbers too |
| `wrapper` | Only for `intest` and `extest` on the wrapper `ff.py wrap` puts on: the `mode`, the number of boundary `cells`, and per part -- `core` (in EXTEST, the core blackbox's boundary nets alone), `boundary` (the boundary cells' faults, an input cell's port bit among them) and `mode` (the mode pins') -- `detected`, `denominator`, `decoupled` (left to the other mode, `excluded_wbr_decoupled`), `blackbox_unresolved`, `hold_unresolved` and `coverage_percent`. The parts add up to the `summary` totals. `coverage.rpt` shows the same as a table |

### The `summary` block

| Field | Meaning |
|---|---|
| `total_raw_faults` | Every enumerated fault, including all tagged exclusions |
| `structural_eligible` | Faults eligible by structure: supported cell, not collapsed, not clock/reset (the structural denominator) |
| `denominator` | The effective (testable) denominator: `structural_eligible` minus proven-redundant faults |
| `detected` | Faults detected by the test set |
| `undetected` | Faults neither detected nor proven redundant |
| `redundant` | Faults proven untestable (SAT UNSAT) under the current model |
| `collapsed` | Faults removed by collapsing |
| `excluded_blackbox` | Faults on blackboxed-cell outputs |
| `excluded_clock` | Clock-net faults (unless `include_clock_faults`) |
| `excluded_reset` | Reset-net faults (unless `include_reset_faults`) |
| `excluded_scan`, `excluded_scan_internal`, `excluded_scan_chain` | Scan-cell-internal and scan-path-only faults (scan campaigns) |
| `excluded_jtag` | Scan campaigns with `[scan] nonscan_cells`: faults scan leaves to `ff.py jtag` -- the non-scan cells' own, those whose every path ends at a non-scan flop or `tdo`, and the stuck-ats that would release a flop held in reset. The `combined` block counts every one of them |
| `blackbox_unresolved` | Scan campaigns: faults, counted in `undetected`, that have a test only if a `[blackbox]` instance could be driven, observed or known, which no scan test can do (Tessent's AU.BB) |
| `hold_unresolved` | Scan campaigns with `[scan] hold`: faults, counted in `undetected`, that have a test only if the held inputs, and the non-scan flops they keep in reset, were free (Tessent's AU.PC) |
| `test_coverage_percent` | `detected / denominator x 100` — credits proven-redundant faults by removing them from the denominator; the **headline** figure |
| `fault_coverage_percent` | `detected / structural_eligible x 100` — counts proven-redundant faults against you; the **conservative** figure |
| `coverage_percent` | The headline coverage figure (equal to `test_coverage_percent`) |

```{note}
`total_raw_faults = denominator + redundant + collapsed + excluded_blackbox +
excluded_clock + excluded_reset + excluded_scan + excluded_cross_domain +
excluded_wbr_decoupled + excluded_jtag`. Exclusions
are always *tagged and counted*, never silently dropped, and a zero denominator is
raised as an error rather than producing a NaN.
```

## `patterns.test`

```{important}
Despite being a documented config key (`[atpg] output`, default `patterns.test`),
this file is **not** written by a normal `sim` / `run_atpg` run — combinational or
scan. It is only ever written as an internal fallback inside `scan_check`'s
vector-source resolution, when the optional Quaigh comparison path (`quaigh atpg
... -o patterns.test`) runs because no vectors were found any other way. In
practice it is rarely present — don't rely on it as a routine deliverable.
```

The reliable ways to get generated vectors out are the campaign database
(`faultflow.sqlite`) and `coverage_report.json` below, or
`sim --scan --export-patterns PATH` — a distinct, JSON-format mechanism (see
[Running without the shell](running_without_shell.md)). When a `[blackbox]`
output of unknown value reaches a scan flop, each exported pattern carries an
`unload_mask` parallel to `expected_unload` (per chain, `true` = compare): a
`false` bit is don't-care, since that flop captured the unknown value. When a
non-scan flop settles during the test (a reset synchronizer, see `[scan]
nonscan_cells`), each pattern carries `preamble_cycles`: the scan-clock pulses,
scan enable off and the holds applied, to give before its load. With scan
compression every pattern carries at least one: it arms the decompressor's reseed,
which a load right after the previous unload wouldn't give. It also carries `seed`,
what a tester holds on the compression channels for the whole pattern (seed bit k
on channel bit k); `load_seqs` is the stream the decompressor makes of it. With
`include_reset_faults`, a pattern may set a reset input active in its
`capture_pi_values` to test the reset; it then carries `shift_pi_values`, the values
those inputs hold everywhere but the capture (inactive: a scan flop's reset acts
during shift too). A transition pattern carries `launch`: `loc`, a functional clock
pulse between the load and the capture, or `los`, one more shift with scan enable on
and each chain's `launch_scan_in` bit at its scan input. `capture_pi_values` holds the
primary inputs' values and the primary outputs' expected values at the capture
(but an output a blackbox's unknown value reaches); a bus port's bits are named
`port[i]`. An EXTEST pattern of the wrapper `ff.py wrap` puts on loads and unloads
the wrapper chains alone: it carries `shift_length`, their longest, the shifts its
load and its unload take; the core's chains shift as many bits, uncompared. `write-patterns` writes the export as STIL, the cycles a tester applies
(see [Running without the shell](running_without_shell.md#write-patterns)). External
vectors supplied
with `sim --ext` use the same plain-text format as `patterns.test` and require a
same-stem `.bench` sidecar that fixes the primary-input order.

## `faultflow.sqlite`

The campaign database under `.faultflow/`. It holds the fault list, per-fault
detection records, generated vectors, run metadata, and timing — and is shared by the
combinational and scan campaigns. `status` reads its current state; `--clean` deletes
it. `ff.py jtag` keeps its grades in two side tables, `jtag_runs` and
`jtag_detections`, keyed to the scan campaign's fault rows: a new scan campaign has new
fault rows, so an older JTAG grade simply stops applying.

```{warning}
The database uses the default (rollback) SQLite journal mode, not WAL. WAL is not
reliable on a Windows drive mounted into WSL (`/mnt/c/...`), so do not switch it on
when the workspace lives on such a path.
```

## `oracle_response.json`

Produced by `run` (OpenTestability oracle mode). A flat JSON document — validated
against [`schemas/oracle_response.schema.json`](https://github.com/ranaumarnadeem/faultflow/blob/main/schemas/oracle_response.schema.json)
— carrying `backend`, `coverage_percent`, `fault_coverage_percent`, `vector_count`,
a `terminal_reason` (`target_reached` / `exhausted` / `timeout`), and the
`undetected_faults` and `per_node` arrays. See [External tools](../external_tools.md).
