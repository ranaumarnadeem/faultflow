# Tcl shell & scripting

faultflow exposes an interactive, **Tcl-style command shell** in addition to the batch
CLI. The shell holds a live project session, so you can load a netlist, synthesize,
insert scan, run ATPG, insert test points, and publish netlists step by step — or
drive the whole flow from a script.

## CLI or shell?

Use the **batch CLI** for fixed, repeatable campaigns driven by a `config.ofs` (the
quick start, CI, the batch script in [Examples](examples.md)). Use the **shell** for
interactive, stepwise work and for the features that live only there: test-point
insertion (`add_tp` / `reject_tp`) and netlist publishing (`write_netlist`). The two
surfaces cover the same core operations under different names:

| Operation | CLI | Shell |
|---|---|---|
| Load / synthesize | implicit from `[design]` | `read_netlist`, `synth` |
| Insert scan | `scan` | `add_scan` |
| Check scan | `scan-check` | `check_scan` |
| Run ATPG | `sim` | `run_atpg` |
| Status | `status` | `status` |

## Starting the shell

```bash
python3 ff.py shell                 # interactive REPL
python3 ff.py shell -c config.ofs   # preload a project config
python3 ff.py shell -f flow.tcl     # run a script, then exit
```

Inside the REPL, `help` lists the commands and `help <command>` shows details. Exit
with `quit` or `exit`. Any line that is not a known command is passed through to the
underlying OS shell.

## A scripted flow

Because `shell -f` executes a script non-interactively, a complete campaign can be
captured as a Tcl file. For example, `flow.tcl`:

```tcl
# flow.tcl — synthesize, scan-insert, and run scan ATPG for serial_adder
read_netlist examples/serial_adder.v -top serial_adder
use_lib_cells sky130
synth

add_scan -chains 4 -SI scan_in -SO scan_out -SE scan_en
check_scan

run_atpg -sa -scan -max 20 -target 95
status -scan
report

write_netlist -scan -techmap -o output/serial_adder/serial_adder_scan.v
```

Run it with:

```bash
python3 ff.py shell -f flow.tcl
```

## Command reference

Commands use Tcl single-dash flags (`-chains 4`). They are grouped by category, as
shown by `help`.

### Project

| Command | Summary |
|---|---|
| `read_netlist PATH -top MODULE` | Load Verilog or Yosys JSON (Verilog is synthesized later) |
| `use_lib_cells PROFILE` | Select the PDK profile: `sky130` or `osu035` |
| `synth` | Synthesize loaded Verilog (validates already-synthesized JSON) |
| `add_clock PORT [-off 0\|1]` | Declare a clock domain; `-off` sets the inactive level |
| `report_clocks` | List declared clock domains |
| `add_blackbox INSTANCE` | Model an instance as a test boundary (pseudo-PI/PO) |
| `report_blackbox` | List blackboxed instances |

### Test mode

| Command | Summary |
|---|---|
| `set_testmode functional\|intest\|extest` | Select the IEEE 1500 wrapper test mode |
| `report_testmode` | Show the current wrapper test mode |

### Test point

| Command | Summary |
|---|---|
| `add_tp [-m METRIC] [-t THRESHOLD] [-n MAX_POINTS]` | Insert test points via OpenTestability and re-run ATPG; prints a Baseline / +TP / Δ comparison |
| `reject_tp` | Revert the last test-point iteration (cannot go past the baseline) |

`add_tp` requires a prior `sim`/ATPG campaign and `[testpoint] opentest` configured.
See [External tools](../external_tools.md).

### Scan

| Command | Summary |
|---|---|
| `add_scan -chains N [-max_length N] [-SI NAME] [-SO NAME] [-SE NAME] [-dry_run]` | Insert and stitch generic scan chains |
| `check_scan` | Validate the current scan insertion |

### Run

| Command | Summary |
|---|---|
| `run_atpg [-sa] [-scan] [-max ROUNDS] [-target PERCENT]` | Run native stuck-at ATPG (combinational by default, `-scan` for scan protocol) |
| `status [-scan]` | Show campaign coverage, classification, terminal reason, and timing |
| `report` | Regenerate the unified report (`report.rpt`) |

### Output

| Command | Summary |
|---|---|
| `write_netlist [-scan] [-techmap\|-notech] [-o PATH] [-verify]` | Publish a functional or scanned netlist. `-verify` (requires `-scan -techmap`) simulates the written techmapped netlist against the generic scan design and fails if their outputs differ |
| `write_patterns` | Export ATPG patterns *(reserved; not yet implemented)* |

### Options / session

| Command | Summary |
|---|---|
| `set_option KEY VALUE` / `unset_option KEY` | Set or clear a persistent flow option |
| `show_config` | Show current option overrides and the supported keys |
| `save_session` / `load_session TOP` / `resume TOP` | Checkpoint and restore project state |
| `clean` / `reset` | Remove campaign DB state / clear the in-memory project |

The options accepted by `set_option` are: `atpg.max_rounds`,
`atpg.sat_timeout_seconds`, `report.threshold`, and
`simulation.unsupported_cells`.

```{note}
`write_patterns` (STIL/WGL export) is a placeholder today and raises a clear
"not implemented" error. `write_netlist -verify` (techmap verification) is
implemented — it fault-free-simulates the written techmapped netlist against the
generic scan design over the scan vectors and fails if their outputs diverge.
```

## The Yosys synthesis script

faultflow's other "scripting" surface is the **locked Yosys synthesis template**,
[`faultflow/templates/yosys_synth.tcl.j2`](https://github.com/ranaumarnadeem/faultflow/blob/main/faultflow/templates/yosys_synth.tcl.j2).
It is a Jinja2 template rendered per run into
`output/<top>/.faultflow/generated_scripts/yosys_synth.tcl` and executed with
`yosys -s`:

```tcl
read_verilog -sv {{ verilog }}
hierarchy -check -top {{ top }}
proc
flatten
opt_expr
opt_clean
synth -top {{ top }}
dfflibmap -liberty {{ liberty }}
abc -liberty {{ liberty }}
clean
write_json {{ output_json }}
write_verilog {{ output_verilog }}
```

One synthesis run emits both `write_json` (consumed by the C++ core) and
`write_verilog` (used by the optional iverilog verification gate). The script is
intentionally fixed so the same netlist is used everywhere, with no drift between the
graded netlist and the verified one.
