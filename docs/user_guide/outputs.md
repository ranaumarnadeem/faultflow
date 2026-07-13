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
├── <top>_scan.v           # scan deliverable (after scan + techmap)
├── <top>_scan.json        # scanned generic JSON
├── scan.rpt               # scan-insertion report
└── .faultflow/            # internal workspace (removed by --clean)
    ├── faultflow.sqlite    # campaign database (combinational + scan)
    ├── logs/               # Yosys / nl2bench / quaigh logs
    ├── manifests/
    ├── intermediate/       # <top>.json, <top>_gate.v, coverage_report.json
    ├── verification/       # iverilog verification reports
    └── generated_scripts/  # rendered yosys_synth.tcl
```

In OpenTestability oracle mode (`run`), an `oracle_response.json` is written to the
output root.

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
| `policy` | The active policies (collapsing, clock/reset inclusion, unsupported-cell mode) |
| `summary` | The coverage and fault-count totals (see below) |
| `run` | Per-run statistics, including ATPG round counts and timing |
| `per_node` | Per-net fault counts and detection status |
| `undetected_faults` | The remaining undetected/redundant faults |

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
| `test_coverage_percent` | `detected / denominator x 100` — credits proven-redundant faults by removing them from the denominator; the **headline** figure |
| `fault_coverage_percent` | `detected / structural_eligible x 100` — counts proven-redundant faults against you; the **conservative** figure |
| `coverage_percent` | The headline coverage figure (equal to `test_coverage_percent`) |

```{note}
`total_raw_faults = denominator + redundant + collapsed + excluded_blackbox +
excluded_clock + excluded_reset + excluded_scan + excluded_cross_domain`. Exclusions
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
[Running without the shell](running_without_shell.md)). External vectors supplied
with `sim --ext` use the same plain-text format as `patterns.test` and require a
same-stem `.bench` sidecar that fixes the primary-input order.

## `faultflow.sqlite`

The campaign database under `.faultflow/`. It holds the fault list, per-fault
detection records, generated vectors, run metadata, and timing — and is shared by the
combinational and scan campaigns. `status` reads its current state; `--clean` deletes
it.

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
