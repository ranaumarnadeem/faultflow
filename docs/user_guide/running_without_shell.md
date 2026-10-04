# Running without the shell

faultflow's primary interface is the interactive [Tcl shell](tcl_shell.md). This
page documents the **batch CLI** — the same core operations as one-shot,
non-interactive commands driven entirely by `config.ofs`, for scripts and CI where
a live session isn't needed.

```bash
python3 ff.py <command> [options]
```

The `bin/faultflow` wrapper is exactly equivalent (it execs the project's
`venv/bin/python ff.py "$@"`), so once the venv is on your `PATH` you can write:

```bash
faultflow <command> [options]
```

## Common options

These apply to `init`, `sim`, `intest`, `extest`, `status`, `scan`, `scan-status`,
`scan-check`, `scan-techmap`, and `rule_check`:

| Option | Required | Default | Meaning |
|---|---|---|---|
| `--top NAME` | yes | — | Top module name |
| `-c`, `--config PATH` | no | `config.ofs` | Path to the `.ofs` config file |

`project`, `retarget`, `add-clock`, `shell`, and `run` each take a different shape
of options — see their own sections below.

## `init`

```bash
python3 ff.py init --top <top> -c config.ofs
```

Create the `output/<top>/` workspace and its `.faultflow/` internals (including the
SQLite database), and record the input fingerprint. Run this once before `sim`.

## `sim`

```bash
python3 ff.py sim --top <top> -c config.ofs [options]
```

Run combinational stuck-at (or transition) ATPG and fault simulation. This is the main
command.

| Option | Meaning |
|---|---|
| `--purge` | Remove transient junk inside `.faultflow/` before the run |
| `--clean` | Remove the entire `.faultflow/` internal workspace first; deliverables under `output/<top>/` are kept |
| `-v`, `--verify VALUE` | Override `[simulation] verify` for this run (a boolean) |
| `--ext PATH` | Use external `.test` vectors instead of ATPG; requires a same-stem `.bench` sidecar for PI order |
| `--max ROUNDS` | Maximum progressive ATPG rounds (default: `[atpg] max_rounds`, which itself defaults to 20); must be ≥ 1 |
| `--scan` | Run full-scan stuck-at ATPG on the reduced pseudo-PI/PO view |
| `--serial-ref` | Run isolated serial reference diagnostics; does not update production coverage. Cannot be combined with `--ext` |
| `-t PCT` | Target coverage percent at which to stop ATPG (default: `[report] threshold`); must be in `(0, 100]` |
| `--model {stuck-at,transition}` | Override `[fault_model] model` for this run. `transition` with `collapsing = true` is rejected |
| `--export-patterns PATH` | Export scan ATPG patterns as JSON to `PATH` (requires `--scan`); input for `retarget --patterns` and `write-patterns --patterns` |

The `--ext` sidecar (`.bench`) is a BENCH-format netlist of the same circuit; faultflow
reads it only to recover the primary-input ordering that the `.test` vector columns map
onto.

## `intest`

```bash
python3 ff.py intest --top <top> -c config.ofs [options]
```

IEEE 1500 INTEST: scan-integrated wrapper coverage of the core. Equivalent to
`sim --scan` with the test mode forced to `intest`.

| Option | Meaning |
|---|---|
| `--purge` | Remove transient junk inside `.faultflow/` before the run |
| `--clean` | Remove the `.faultflow/` internal workspace before the run; deliverables are kept. Needed when switching between `intest`/`extest` — test mode is part of the campaign fingerprint |
| `--max ROUNDS` | Maximum progressive ATPG rounds (default: `[atpg] max_rounds`) |
| `-t PCT` | Target coverage percent (default: `[report] threshold`) |

## `extest`

```bash
python3 ff.py extest --top <top> -c config.ofs [options]
```

IEEE 1500 EXTEST: wrapper-boundary / interconnect coverage, with the core held
safe (combinational ATPG on the fused boundary view). Takes the same options as
`intest`.

## `project`

```bash
python3 ff.py project -p project.json [--clean] [--max ROUNDS] [-t PCT]
```

Hierarchical project flow: runs per-block INTEST plus an assembly EXTEST and
aggregates one chip-level coverage number, driven by a project manifest JSON. This
command takes **`-p`/`--project` instead of `--top`** — the manifest names the
blocks and assembly itself.

| Option | Required | Meaning |
|---|---|---|
| `-p`, `--project PATH` | yes | Project manifest JSON |
| `--clean` | no | Clean each scope's workspace before running |
| `--max ROUNDS` | no | Maximum progressive ATPG rounds per scope |
| `-t PCT` | no | Target coverage percent per scope |

## `retarget`

```bash
python3 ff.py retarget --patterns PATH --soc-access PATH --block NAME --out PATH
```

Retarget a block's exported scan patterns (from `sim --scan --export-patterns`)
onto a SoC scan path described by a SoC-access manifest. Writes a
`faultflow_retargeted_v1` pattern file. No re-ATPG happens at the assembly level.

| Option | Required | Meaning |
|---|---|---|
| `--patterns PATH` | yes | Block scan pattern JSON |
| `--soc-access PATH` | yes | SoC access manifest JSON (`faultflow_soc_access_v1`) |
| `--block NAME` | yes | Source block name |
| `--out PATH` | yes | Output retargeted pattern file |

```{note}
The Tcl shell's equivalent command, also named `retarget`, uses single-dash
Tcl-style flags (`-patterns`, `-soc_access`) instead of the CLI's
`--patterns`/`--soc-access` — same operation, different flag syntax.
```

## `write-patterns`

```bash
python3 ff.py write-patterns --top <top> -c config.ofs --patterns patterns.json -o chip.stil
```

Write the scan patterns `sim --scan --export-patterns` exported as STIL (IEEE 1450):
the cycles a tester applies to the chip, one pattern after another. Each load also
unloads the pattern before, the usual way, with half the shifts: the preamble is
given once, first, and a last unload ends the patterns. With `--no-overlap` each
pattern is applied alone, as FaultFlow grades it -- its preamble, load, launch,
capture and unload. Nothing an unload compares depends on what shifts in at the
same time or on the inputs a load holds, the holds keep every flop a preamble
settles settled, and each capture's pulse arms the decompressor's reseed for the
next load, so both give the same results; the test suite checks both on the cells.
The chip is the scanned netlist, or with scan compression or compaction the one
`scan-compress` or `scan-compact` wrote (with both, the compacted one). A design
with IEEE 1500 wrapper cells is refused: they have no cell-level implementation.
The Tcl shell's `write_patterns -patterns PATH -o PATH [-no_overlap]` does the same.

| Option | Required | Meaning |
|---|---|---|
| `--patterns PATH` | yes | The patterns `sim --scan --export-patterns` wrote for this scan campaign |
| `-o PATH`, `--output PATH` | yes | The STIL file to write |
| `--no-overlap` | no | Apply each pattern alone: its unload doesn't overlap the next load |

What the STIL holds:

- A signal per pin bit (a bus bit is `"port[i]"`), in groups: `_pi` (the inputs but
  the scan clocks, scan enable and scan inputs), `_po` (the outputs but the scan
  outputs), `_si` (the scan inputs; with compression, the channels a pattern's seed
  is held on), `_so` (the scan outputs; with compaction, the compactor's channels),
  `_clk` (the scan clocks) and `_se`.
- One WaveformTable, `_wft_`: a 100 ns period, inputs changing at 0 ns, outputs
  strobed at 40 ns, the scan clocks pulsing high from 50 ns to 80 ns.
- Without compression or compaction, ScanStructures: each chain's scan input and
  output, length and cells, scan input first.
- One procedure, `load_unload`, which sets every signal itself, since readers
  differ on whether a procedure keeps its caller's signal states: a call passes it
  the inputs' values to hold (with compression, the seed too), it turns scan enable
  on, then a Shift takes a bit per scan input and per scan output each shift. With
  compression the scan inputs aren't shifted: the decompressor makes the load from
  the seed they hold.
- Per pattern: a `load_unload` call for its load -- comparing the unload of the
  pattern before, or nothing -- a vector for a transition pattern's launch and one
  for its capture (the outputs strobed); then a last `load_unload` call for the
  last unload.
  An unload compares the expected bits, X where a flop captured an unknown value or
  the decompressor's bits come out of a shorter chain. The preamble is a Loop, once
  first, or before each pattern's load with `--no-overlap`.

These are exactly the cycles the test suite replays on the chip's sky130 cell models,
and an independent STIL parser, Semi-ATE-STIL, reads every file it writes.

## `status`

```bash
python3 ff.py status --top <top> -c config.ofs [--scan]
```

Print the current coverage status from the campaign database.

| Option | Meaning |
|---|---|
| `--scan` | Report status from the scan campaign instead of the combinational one |

## `scan`

```bash
python3 ff.py scan --top <top> -c config.ofs [options]
```

Insert and stitch generic scan chains.

| Option | Meaning |
|---|---|
| `--scan-chains N` | Requested scan-chain count |
| `--max-chain-length N` | Maximum flip-flops per chain (may increase the chain count) |
| `-SI`, `--scan-in NAME` | Scan input port base name |
| `-SO`, `--scan-out NAME` | Scan output port base name |
| `-SE`, `--scan-enable NAME` | Scan enable port name |
| `--techmap` / `--no-techmap` | Run / skip the Sky130 scan-cell techmap after stitching |
| `--dry-run` | Print the scan plan only |

## `scan-status`

```bash
python3 ff.py scan-status --top <top> -c config.ofs
```

Print scan-insertion status.

## `scan-check`

```bash
python3 ff.py scan-check --top <top> -c config.ofs [options]
```

Validate the inserted scan chains (structural and normal-mode checks). It also fails
on a scan flop whose asynchronous clear or preset isn't held inactive while the chains
shift -- a scanned reset synchronizer's output, say: a real scan flop's clear and preset
act during shift, which FaultFlow's simulation doesn't model. The error names each flop
and what drives its pin; see `[scan] shift_controls` in [Configuration](configuration.md).

| Option | Meaning |
|---|---|
| `--vectors PATH` | Explicit `.test` vector file |
| `--require-techmap` | Require the generated Sky130 Verilog artifact to be present |

## `scan-techmap`

```bash
python3 ff.py scan-techmap --top <top> -c config.ofs
```

Regenerate the Sky130 techmap output from the scanned JSON.

## `jtag`

```bash
python3 ff.py jtag --top <top> -c config.ofs [--program FILE] [--verify] [--force]
```

Grade the scan campaign's stuck-at faults with a JTAG network-integrity program played
through the TAP of the scanned netlist and compared at TDO, and report the credit beside
the scan coverage (the `jtag` and `combined` blocks, see [Outputs](outputs.md)). Needs a
completed `sim --scan`; stuck-at only. With `[compression]` or `[compaction]`, their
`channel_port` can't be a TAP pin's name (the defaults, `tdi` and `tdo`, are).

The program is a `warptap-tck-program` JSON file (`--program` or `[jtag] program`), or,
for a design built from an autoMBIST manifest with `test_access`, it's built from that
network with warptap's `build_integrity_program`, reaching the network the way the
manifest's TAP does -- EXTEST, or a dedicated IJTAG_ACCESS instruction (`mbist-insert`)
-- and reading the manifest's IDCODE. A program file must reach the network the same
way. Before grading, faultflow proves TDO can't see an unknown value and checks the
netlist's own TDO against the program on every bit it expects; either failure refuses
the run and writes nothing. The proof: every TAP flop is reset by `trst_n` at 0; every
other flop sits still at a known reset value, or its output can't reach TDO; blackbox
outputs can't either. A reset is followed through buffers, inverters and any AND/OR-type
gate an input holds at its controlling value (`trst_n & clr_n` at `trst_n` = 0), and up
a chain of flops (a reset synchronizer resetting a collar).

The inputs the program doesn't drive are held, at 0 or at the level that keeps a flop's
reset active (`[jtag] hold` overrides) -- except the chip reset of a manifest whose
control TDRs also clear on it: holding it active would keep them at 0, so the program
pulses it during its TRST lead-in, then holds it inactive.

With the TAP non-scan (`[scan] nonscan_cells`), the faults scan left to JTAG
(`excluded_jtag`) are graded too, and the `combined` block counts every one of them,
detected or not.

| Option | Meaning |
|---|---|
| `--program FILE` | TCK program to play, instead of `[jtag] program` or building one |
| `--verify` | Also replay the program on the techmapped netlist in Icarus Verilog, four-state, and require the golden TDO at every shift |
| `--force` | Grade again when an identical grade (program, netlist, holds) is recorded |
| `--threads N` | Grading threads (default: `[simulation] sim_threads`) |
| `--export FILE` | Write the TCK program played |

## `rule_check`

```bash
python3 ff.py rule_check --top <top> -c config.ofs [--strict | --advisory]
```

Run DFT structural rules (a design rule check) on the synthesized netlist. Writes
`output/<top>/rule_check.rpt`. Has no Tcl shell equivalent.

| Option | Meaning |
|---|---|
| `--strict` | Treat warnings as blocking (non-zero exit) |
| `--advisory` | Report violations but always exit 0 (no gate) |

By default the command exits non-zero only if a blocking violation is found.

## `add-clock`

```bash
python3 ff.py add-clock <port> [-c config.ofs] [--off 0|1]
```

Declare a clock domain by **editing `config.ofs`'s `[clocks]` section** — a
one-shot CLI counterpart to the Tcl shell's session-scoped `add_clock`. Unlike the
shell command, this doesn't touch a live session; it writes the declaration to the
config file so subsequent `init` / `sim` / etc. runs pick it up.

| Argument | Required | Default | Meaning |
|---|---|---|---|
| `port` (positional) | yes | — | Clock port name |
| `-c`, `--config PATH` | no | `config.ofs` | Config file to edit |
| `--off {0,1}` | no | `0` | Clock's inactive level (`0`=active-high/posedge, `1`=negedge) |

## `list-memories`

```bash
python3 ff.py list-memories --top <top> --spec mbist.yml [--pattern GLOB ...]
```

List the memory macro instances of the design an MBIST insertion file names, to help
write it: each instance, whether the file configures it, each pin with what drives or
reads it, and an entry to paste. `--pattern` (repeatable) replaces the file's
`memory_patterns`. See [MBIST insertion](mbist.md).

## `mbist-insert`

```bash
python3 ff.py mbist-insert --top <top> --spec mbist.yml [--out DIR] [-c chip.ofs] [--tap-nonscan]
```

Insert an autoMBIST collar, in a shell, in place of every memory the insertion file
configures, keeping the design's hierarchy; with `jtag` in the file, behind a TAP and an
IJTAG network, with the BIST program. With `-c`, also synthesize the result, its DFT
frozen, and write `<top>_mbist.ofs` for the scan and JTAG flow; `--tap-nonscan` runs the
TAP and the network non-scan there. See [MBIST insertion](mbist.md).

| Option | Default | Meaning |
|---|---|---|
| `--out DIR` | `mbist_<top>` | Where everything is written |
| `-c`, `--config PATH` | — | The design's `.ofs`: its liberty names cells no new module may take, synthesizes the result, and is merged into `<top>_mbist.ofs` |
| `--tap-nonscan` | off | With `jtag` and `-c`: the TAP and the network non-scan in the written `.ofs`, `trst_n` and `tck` held at 0 |

## `run` (OpenTestability oracle mode)

```bash
python3 ff.py run -c <ot.ofs>
```

Oracle mode for [OpenTestability](../external_tools.md): translate an OT-format
`.ofs`, run ATPG, and write `oracle_response.json`. The top module is taken from the
OT config, so there is **no `--top`** here. Has no Tcl shell equivalent.

| Option | Required | Meaning |
|---|---|---|
| `-c`, `--config PATH` | yes | Path to an OpenTestability-format `.ofs` |

## `shell`

```bash
python3 ff.py shell [options]
```

Start the interactive Tcl shell. See [The Tcl shell](tcl_shell.md) for the full
command reference.

| Option | Default | Meaning |
|---|---|---|
| `-f`, `--file PATH` | — | Execute a Tcl script non-interactively, then exit |
| `-c`, `--config PATH` | — | Preload a project config (reads `[design] top`) |
| `--out PATH` | `output` | Output root directory |
| `--verbose` | off | More verbose result formatting |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (or `rule_check --advisory`) |
| `1` | Oracle (`run`) failure; a blocking `rule_check` violation |
| `2` | Configuration or runner error |

## Shell ↔ CLI mapping

Both surfaces cover most of the same ground; a few operations exist only on one
side.

| Operation | Tcl shell | Batch CLI |
|---|---|---|
| Load / synthesize | `read_netlist`, `synth` | implicit from `[design]` |
| Combinational ATPG | `run_atpg` | `sim` |
| Insert scan | `add_scan` | `scan` |
| Check scan | `check_scan` | `scan-check` |
| Scan ATPG | `add_scan` + `check_scan` + `run_atpg -scan` | `sim --scan` |
| Scan status | `status -scan` | `scan-status` / `status --scan` |
| Regenerate techmap | `write_netlist -scan -techmap` | `scan-techmap` |
| JTAG network-integrity grade | `run_jtag` | `jtag` |
| Status | `status` | `status` |
| INTEST | `set_testmode intest` + scan flow + `run_atpg -scan` | `intest` |
| EXTEST | `add_blackbox` ... + `set_testmode extest` + `run_atpg` | `extest` |
| Hierarchical SoC | `flowscripts/hereichy_atpg.tcl` | `project` |
| Retarget to SoC | `retarget -patterns ... -soc_access ...` | `retarget --patterns ... --soc-access ...` |
| Write patterns as STIL | `write_patterns -patterns ... -o ...` | `write-patterns --patterns ... -o ...` |
| Declare a clock | `add_clock` (live session) | `add-clock` (edits `config.ofs`) |
| DFT rule check | *(shell has no equivalent)* | `rule_check` |
| OpenTestability oracle | *(shell has no equivalent)* | `run` |
| Test-point insertion | `add_tp` / `reject_tp` | *(CLI has no equivalent)* |
| Regenerate report | `report` | written automatically by `sim` |
