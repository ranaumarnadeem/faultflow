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
| `--export-patterns PATH` | Export scan ATPG patterns as JSON to `PATH` (requires `--scan`); input for `retarget --patterns` |

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

Validate the inserted scan chains (structural and normal-mode checks).

| Option | Meaning |
|---|---|
| `--vectors PATH` | Explicit `.test` vector file |
| `--require-techmap` | Require the generated Sky130 Verilog artifact to be present |

## `scan-techmap`

```bash
python3 ff.py scan-techmap --top <top> -c config.ofs
```

Regenerate the Sky130 techmap output from the scanned JSON.

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
| Status | `status` | `status` |
| INTEST | `set_testmode intest` + scan flow + `run_atpg -scan` | `intest` |
| EXTEST | `add_blackbox` ... + `set_testmode extest` + `run_atpg` | `extest` |
| Hierarchical SoC | `flowscripts/hereichy_atpg.tcl` | `project` |
| Retarget to SoC | `retarget -patterns ... -soc_access ...` | `retarget --patterns ... --soc-access ...` |
| Declare a clock | `add_clock` (live session) | `add-clock` (edits `config.ofs`) |
| DFT rule check | *(shell has no equivalent)* | `rule_check` |
| OpenTestability oracle | *(shell has no equivalent)* | `run` |
| Test-point insertion | `add_tp` / `reject_tp` | *(CLI has no equivalent)* |
| Regenerate report | `report` | written automatically by `sim` |
