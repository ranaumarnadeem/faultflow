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
| `collapsing` | bool | `false` | Enable equivalence/dominance fault collapsing. `transition` + `collapsing=true` is an error |
| `include_clock_faults` | bool | `false` | Count clock-net faults in the denominator |
| `include_reset_faults` | bool | `false` | Count reset-net faults in the denominator |

## `[simulation]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `unsupported_cells` | `fail`, `blackbox` | `fail` | Policy when a cell is not in the cell map (see below) |
| `verify` | bool | `false` | Run the optional iverilog verification gate |
| `verify_tool` | `iverilog` | `iverilog` | Verification backend |
| `tie_xz` | bool | `false` | Tie Yosys `x`/`z` constant bits to 0 |
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
| `output` | path | `patterns.test` | Generated pattern file name (under `output/<top>/`) |
| `random_vectors` | int | `64` | Random vectors applied before targeted ATPG |
| `sat_conflict_limit` | int | `100000` | Per-fault CaDiCaL conflict limit |
| `max_rounds` | int | `20` | Maximum progressive ATPG rounds |
| `sat_timeout_seconds` | int | `10` | Per-fault SAT timeout |
| `compaction` | `none`, `reverse` | `reverse` | Test-set compaction (reverse-order, coverage-preserving) |

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

Declare multiple clock domains (also settable interactively with `add_clock`).

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

## Notes on legacy and inert keys

```{note}
Some keys that appear in older example files are currently **not read** by the
parser and have no effect: `[simulation] tp_file`, `[report] verbose`, and
`[debug] level`. The CLI also does **not** implement `--debug`/`--trace`; logging is
at INFO level. These are documented here only so you are not surprised to see them in
sample configs.
```
