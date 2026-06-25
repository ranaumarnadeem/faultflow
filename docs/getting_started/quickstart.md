# Quick start

This walkthrough runs a complete stuck-at fault campaign on **`c17`**, the smallest
ISCAS-85 benchmark (5 inputs, 2 outputs, 6 NAND gates). It uses the pre-synthesized
netlist shipped under `tests/benchmarks/`, so **no Yosys run is required** — a good
first smoke test.

```{note}
Make sure you have [built the C++ core](installation.md) (`cmake --build build`)
before running these commands.
```

## 1. Create a config

The repository ships a complete template. Copy it to `config.ofs`:

```bash
cp config.ofs.example config.ofs
```

Out of the box it points at the Sky130-mapped `c17` netlist:

```ini
[design]
netlist   = tests/benchmarks/iscas85/synth_sky130/c17.json
top       = c17
cell_lib  = cells/sky130/sky130_fd_sc_hd.json
liberty   = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib

[atpg]
tool      = native      # built-in SAT ATPG

[report]
threshold = 95.0
```

The full set of options is documented in the
[Configuration reference](../user_guide/configuration.md).

## 2. Initialize the workspace

```bash
python3 ff.py init --top c17 -c config.ofs
```

`init` creates `output/c17/` and its internal `.faultflow/` workspace (including the
SQLite campaign database) and records a fingerprint of the inputs.

## 3. Run ATPG + fault simulation

```bash
python3 ff.py sim --top c17 -c config.ofs
```

This enumerates SA0/SA1 faults, runs the native SAT ATPG progressive loop to generate
test vectors, fault-simulates them with the bit-parallel engine, and stops when it
reaches the coverage threshold (or exhausts/stalls). Results are written to the
campaign database and to the human-readable report.

## 4. Read the coverage

```bash
python3 ff.py status --top c17 -c config.ofs
```

`status` prints the coverage headline (test coverage, fault coverage, detected vs.
undetected vs. redundant, and the terminal reason). The full report is written to:

```text
output/c17/coverage.rpt          # human-readable text report
output/c17/patterns.test         # generated test vectors
output/c17/.faultflow/faultflow.sqlite   # campaign database
```

The machine-readable JSON report (validated against a schema) is at
`output/c17/.faultflow/intermediate/coverage_report.json`. The fields are explained
in [Outputs](../user_guide/outputs.md).

## Re-running cleanly

To wipe the internal workspace and start the campaign fresh while keeping the
deliverables in `output/c17/`:

```bash
python3 ff.py sim --top c17 -c config.ofs --clean
```

`--purge` is a lighter variant that only removes transient junk inside `.faultflow/`.

## Where to go next

- [Examples](../user_guide/examples.md) — a combinational adder synthesized from
  Verilog, a sequential design with scan, and the PicoRV32 core.
- [CLI reference](../user_guide/cli_reference.md) — every command and flag.
- [Concepts](concepts.md) — what stuck-at faults, ATPG, and the coverage denominator
  actually mean.
