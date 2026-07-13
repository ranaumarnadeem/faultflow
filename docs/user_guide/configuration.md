# Configuration reference

faultflow is configured with an **INI-style** file conventionally named
`config.ofs` and passed with `-c`. It is parsed with Python's `configparser`. The
top module always comes from `--top` on the command line, not from the file.

Booleans accept `true/false`, `1/0`, `yes/no`, or `on/off`.

The canonical, fully-commented template is
[`config.ofs.example`](https://github.com/ranaumarnadeem/faultflow/blob/main/config.ofs.example).
Copy it and edit:

```bash
cp config.ofs.example config.ofs
```

```{note}
The OpenTestability **oracle** mode (`faultflow run -c ot.ofs`) reads a small,
differently-shaped handoff file, not this schema — see "OpenTestability — test-
point insertion" under [External tools](../external_tools.md).
```

## A complete example

```ini
[design]
netlist   = tests/benchmarks/iscas85/synth_sky130/c17.json
top       = c17
cell_lib  = cells/sky130/sky130_fd_sc_hd.json
liberty   = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib
yosys_ver = 0.61+129

[fault_model]
model                = stuck_at
launch               = loc
collapsing           = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
verify            = false
verify_tool       = iverilog
verilog_models    = cells/sky130/sky130_fd_sc_hd.v

[atpg]
tool                = native
mode                = comb
output              = patterns.test
random_vectors      = 64
sat_conflict_limit  = 100000
max_rounds          = 20
sat_timeout_seconds = 10
compaction          = reverse

[report]
output    = coverage.rpt
threshold = 95.0

[scan]
chains      = 1
scan_in     = scan_in
scan_out    = scan_out
scan_enable = scan_en
run_techmap = true
```

## `[design]`

| Key | Default | Meaning |
|---|---|---|
| `netlist` | `design.json` | Input Yosys JSON or Verilog netlist |
| `cell_lib` | `cells/sky130/sky130_fd_sc_hd.json` | JSON cell map consumed by the C++ core |
| `liberty` | Sky130 HD `.lib` | Liberty file — used by **Yosys only** |
| `verilog_models` | — | Behavioral cell models for the iverilog gate (falls back to `[simulation] verilog_models`) |
| `yosys_ver` | `""` | Yosys version string, recorded in the fingerprint |

```{note}
The cell map and Liberty must be a matched pair for the same PDK. The two shipped pairs
are: **Sky130 HD** — `cell_lib = cells/sky130/sky130_fd_sc_hd.json`,
`liberty = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib`; and **OSU035** —
`cell_lib = cells/osu/osu035.json`, `liberty = cells/osu/osu035_stdcells.lib`.
```

## `[fault_model]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `model` (alias `type`) | `stuck_at`, `transition` | `stuck_at` | Fault model. `model` is preferred; `type` is a back-compat alias |
| `launch` | `loc`, `los` | `loc` | Transition launch style for **scan** transition ATPG. `los` (launch-on-shift) is supported for the scan flow only; combinational broadside transition rejects `los` |
| `collapsing` | bool | `true` (see note) | Enable equivalence-only fault collapsing — never dominance, so coverage is provably unchanged (see [Collapsing rules](../collapsing_rules.md)). `transition` + `collapsing=true` is an error |
| `include_clock_faults` | bool | `false` | Count clock-net faults in the denominator |
| `include_reset_faults` | bool | `false` | Grade async reset/set-tree faults via implication instead of excluding them. Fingerprinted — toggling it forces a fresh campaign |

```{important}
`collapsing` defaults to **true** (on) when the key is omitted entirely.
`config.ofs.example` and every shipped example config set it to `false`
explicitly, so out-of-the-box runs have collapsing **off** — but deleting that
line rather than setting it turns collapsing back on.
```

## `[simulation]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `unsupported_cells` | `fail`, `blackbox` | `fail` | Policy when a cell is not in the cell map (see below) |
| `verify` | bool | `false` | Run the optional iverilog verification gate |
| `verify_tool` | `iverilog` | `iverilog` | Verification backend |
| `verify_use_power_pins` | bool | `false` | Drive `VPWR=1`/`VGND=0` and pass `-DUSE_POWER_PINS` in the generated testbench, for behavioral models that need it |
| `tie_xz` | bool | `false` | Tie Yosys `x`/`z` constant bits to 0 before simulation (needed for netlists with unconnected/don't-care inputs, e.g. unused scan pins) |
| `sim_threads` | int | `1` | Threads for parallel fault grading, the dominant ATPG cost. `1` = serial, `0` = auto (`cpu_count - 2`, min 1), `N` = `N` threads. Coverage is bit-identical for any value — only wall-clock changes |
| `verilog_models` | — | Sky130 models | Behavioral cell models for verification |

```{admonition} Unsupported-cell policy
:class: important
With `fail` (the default), faultflow aborts immediately if the netlist contains any
cell not in the cell map — coverage is never reported on an unknown netlist. With
`blackbox`, unknown cells are explicitly modeled as test boundaries: their outputs
get no fault sites and are excluded from the denominator (and reported separately).
There is no silent-skip option.
```

## `[atpg]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `tool` | `native`, `sat_atpg`, `quaigh` | `native` | ATPG backend. `native` is the built-in SAT ATPG |
| `mode` | `comb` | `comb` | Only combinational mode is supported |
| `output` | path | `patterns.test` | Fallback pattern file name (under `output/<top>/`). Not produced by a normal `sim`/`run_atpg` run — see [Outputs & reports](outputs.md) |
| `random_vectors` | int | `64` | Random vectors applied before targeted ATPG |
| `random_stop_coverage` | float | `85.0` | Stop grading further random vectors and switch to SAT once coverage reaches this percent — or once the full `random_vectors` budget is graded, whichever comes first. `0.0` disables the early switch |
| `sat_conflict_limit` | int | `100000` | Per-fault CaDiCaL conflict limit |
| `max_rounds` | int | `20` | Maximum progressive ATPG rounds |
| `sat_timeout_seconds` | int | `10` | Per-fault SAT timeout; the fallback used when `sat_timeout_schedule` is empty |
| `sat_timeout_schedule` | comma list | `2,10,60` | Escalating per-fault SAT timeout, smallest tier first — a fault only escalates to the next tier when it times out at the current one. Empty uses the fixed `sat_timeout_seconds` for every fault |
| `compaction` | `none`, `reverse`, `dynamic` | `reverse` | Test-set compaction after ATPG. `reverse` is reverse-order, fault-sim-verified static compaction (coverage-preserving); `dynamic` is sim-verified cube-packing |
| `fault_drop_sat` | bool | `true` | Fault-simulate an accepted SAT vector against *all* remaining faults, not just the target fault, so incidental "fallout" detections are dropped too |
| `cone_restrict` | bool | `true` | Restrict each fault's CNF to its structural cone of influence |
| `incremental_sat` | bool | `false` | Use incremental-SAT (IFC) solving, one observable cone at a time |
| `pack_orders` | int | `1` | Number of packing orders tried during dynamic compaction |
| `order_by_cone_size` | bool | `true` | Order faults by ascending structural cone size so the fastest SAT calls run first, incidentally detecting more faults early. Coverage-identical; stuck-at only |
| `workers` | int | `1` | Parallel SAT worker processes (`1` = serial). Requires a fork-capable OS (Linux/WSL); silently falls back to serial on Windows/spawn platforms. Also settable interactively in the shell with `WORKERS N` |
| `preflight` | bool | `true` | Run OpenTestability structural reconvergence analysis before ATPG (reconvergent-site faults sorted last and skip the short timeout tier; canceling-path-stem faults pre-certified UNSAT with no SAT call). Requires the `opentest` binary on `PATH`; falls back silently when unavailable |
| `preflight_tech` | str | `""` (auto-detect) | PDK tech tag passed to `opentest`'s preflight pass. Empty auto-detects from the `cell_lib` path (`sky130` unless the path contains `osu035`/`osu`) |
| `easy_fault_reserve` | int | `2` | Easy/hard worker split, applied only when `workers >= 4`: each wave reserves this many slots for easy (small-cone, non-reconvergent) faults while the rest run hard faults, so fast and slow faults progress concurrently. `0` disables the split |

## `[report]`

| Key | Default | Meaning |
|---|---|---|
| `output` | `coverage.rpt` | Human-readable report file name |
| `threshold` | `95.0` | Target coverage percent at which ATPG stops |

## `[scan]`

| Key | Default | Meaning |
|---|---|---|
| `chains` | `1` | Number of scan chains |
| `max_chain_length` | (none) | Maximum flip-flops per chain; empty means unbounded |
| `scan_in` | `scan_in` | Scan input port base name |
| `scan_out` | `scan_out` | Scan output port base name |
| `scan_enable` | `scan_en` | Scan enable port name |
| `run_techmap` | `true` | Run the Sky130 scan-cell techmap after stitching |

## `[testpoint]`

Used by the `add_tp` command in the [Tcl shell](tcl_shell.md) to drive
OpenTestability.

| Key | Default | Meaning |
|---|---|---|
| `opentest` | `opentest` | Path to the `opentest` binary |
| `metric` | `scoap` | Testability metric |
| `threshold` | `50` | Target threshold passed to OpenTestability |
| `max_points` | `10` | Maximum test points to insert |

## `[clocks]`

Declare multiple clock domains (also settable interactively with `add_clock`, or
one-shot from the CLI with `add-clock`).

| Key | Format | Meaning |
|---|---|---|
| `ports` | comma list, e.g. `clk_a, clk_b` | Clock port names |
| `off` | comma list of `port:0\|1` | Per-port inactive level |

## `[blackbox]`

| Key | Format | Meaning |
|---|---|---|
| `instances` | comma list, e.g. `u_sram, u_pll` | Instances modeled as test boundaries (pseudo-PI/PO) |

## `[testmode]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `mode` | `functional`, `intest`, `extest` | `functional` | IEEE 1500 wrapper test mode |

## `[wrap]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `wbr_model` | `buffer`, `scan` | `scan` | Wrapper boundary register model used by the shell's `wrap` command: `buffer` is transparent, `scan` is a native shiftable WBR |

## Notes on legacy and inert keys

```{note}
Some keys that appear in `config.ofs.example` are currently **not read** by the
parser and have no effect: `[simulation] tp_file`, `[report] verbose`, and the
whole `[debug]` section. The CLI also does **not** implement `--debug`/`--trace`;
logging is at INFO level. These are called out here only so you are not surprised
to see them in sample configs — extra observation points come from a separate
`tp_nodes.json` file (see [Architecture overview](../architecture/overview.md)),
not `tp_file`.
```
