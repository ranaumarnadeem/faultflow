# Command-line reference

faultflow is driven from the `ff.py` entry point:

```bash
python3 ff.py <command> [options]
```

The `bin/faultflow` wrapper is exactly equivalent (it execs the project's
`venv/bin/python ff.py "$@"`), so once the venv is on your `PATH` you can write:

```bash
faultflow <command> [options]
```

```{note}
There are two command surfaces. This page documents the **batch CLI** (`init`,
`sim`, `status`, scan commands, `rule_check`, `run`). The `shell` command opens an
**interactive Tcl shell** with its own command set, documented separately in
[Tcl shell & scripting](tcl_shell.md).
```

## Common options

These apply to `init`, `sim`, `status`, `scan`, `scan-status`, `scan-check`,
`scan-techmap`, and `rule_check`:

| Option | Required | Default | Meaning |
|---|---|---|---|
| `--top NAME` | yes | — | Top module name |
| `-c`, `--config PATH` | no | `config.ofs` | Path to the `.ofs` config file |

`shell` and `run` take different options (see below).

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

The `--ext` sidecar (`.bench`) is a BENCH-format netlist of the same circuit; faultflow
reads it only to recover the primary-input ordering that the `.test` vector columns map
onto.

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
`output/<top>/rule_check.rpt`.

| Option | Meaning |
|---|---|
| `--strict` | Treat warnings as blocking (non-zero exit) |
| `--advisory` | Report violations but always exit 0 (no gate) |

By default the command exits non-zero only if a blocking violation is found.

## `run` (OpenTestability oracle mode)

```bash
python3 ff.py run -c <ot.ofs>
```

Oracle mode for [OpenTestability](../external_tools.md): translate an OT-format
`.ofs`, run ATPG, and write `oracle_response.json`. The top module is taken from the
OT config, so there is **no `--top`** here.

| Option | Required | Meaning |
|---|---|---|
| `-c`, `--config PATH` | yes | Path to an OpenTestability-format `.ofs` |

## `shell`

```bash
python3 ff.py shell [options]
```

Start the interactive Tcl shell. See [Tcl shell & scripting](tcl_shell.md).

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
