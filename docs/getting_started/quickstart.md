# Quick start

This walkthrough grades stuck-at faults on **`c17`**, the smallest ISCAS-85
benchmark (5 inputs, 2 outputs, 6 NAND gates), from the interactive **Tcl shell** —
faultflow's primary interface. It uses the pre-synthesized netlist shipped under
`tests/benchmarks/`, so **no Yosys run is required**: a good first smoke test.

```{note}
Make sure you have [built the C++ core](installation.md) (`cmake --build build`)
before running these commands.
```

## 1. Launch the shell

```bash
python3 ff.py shell
```

This drops you into an interactive REPL. The prompt starts bare (`faultflow> `) and
grows to show the loaded design and PDK as you go (`faultflow[c17:sky130]> `). Every
command below is typed at that prompt.

## 2. Load and prepare the design

```tcl
faultflow> read_netlist tests/benchmarks/iscas85/synth_sky130/c17.json -top c17
faultflow[c17]> use_lib_cells sky130
faultflow[c17:sky130]> synth
```

- `read_netlist` loads a Verilog or Yosys-JSON netlist. `c17.json` here is already
  synthesized, but the same command works for a `.v` file — synthesis just happens
  in the next step instead of being a no-op.
- `use_lib_cells` selects the PDK cell semantics (`sky130` or `osu035`).
- `synth` runs Yosys on loaded Verilog; on already-synthesized JSON it validates the
  netlist and switches the active source to it. Always call it, regardless of which
  kind of input you loaded — that keeps one command sequence that works for both.

## 3. Run ATPG

```tcl
faultflow[c17:sky130]> run_atpg -sa -target 95
```

This enumerates SA0/SA1 faults, runs the native SAT ATPG progressive loop
(CaDiCaL) to generate test vectors, fault-simulates them with the bit-parallel
engine, and stops once it reaches 95% coverage (or exhausts/stalls first).

## 4. Read the coverage

```tcl
faultflow[c17:sky130]> status
faultflow[c17:sky130]> report
```

`status` prints the coverage headline (test coverage, fault coverage, detected vs.
undetected vs. redundant, and the terminal reason). `report` regenerates the
unified report on disk. Both the shell and the CLI write into the same per-design
workspace:

```text
output/c17/coverage.rpt          # human-readable text report
output/c17/report.rpt            # unified report (written by the shell `report` command)
output/c17/.faultflow/faultflow.sqlite   # campaign database
```

The machine-readable JSON report (validated against a schema) is at
`output/c17/.faultflow/intermediate/coverage_report.json`. The fields are explained
in [Outputs & reports](../user_guide/outputs.md).

Exit with `quit` (or `exit`, or Ctrl-D):

```tcl
faultflow[c17:sky130]> quit
```

## Re-running cleanly

The shell's `clean` command removes the campaign database so you can re-run ATPG
from scratch while keeping manifests, logs, and netlists:

```tcl
faultflow[c17:sky130]> clean
```

`reset` instead clears the in-memory session without touching disk. See
[The Tcl shell](../user_guide/tcl_shell.md) for the full distinction, and the
[batch CLI's `--clean` / `--purge`](../user_guide/running_without_shell.md) if
you're driving the same workspace non-interactively.

## Scripting the same flow

Everything above can be captured in a `.tcl` file and run non-interactively with
`python3 ff.py shell -f flow.tcl` — see [The Tcl shell](../user_guide/tcl_shell.md)
for a scripted example and the ready-to-run recipes under
[Flow recipes](../user_guide/examples.md).

## Where to go next

- [The Tcl shell](../user_guide/tcl_shell.md) — every command, grouped by category.
- [Flow recipes](../user_guide/examples.md) — synthesizing from Verilog, scan
  insertion, and the PicoRV32 core.
- [Running without the shell](../user_guide/running_without_shell.md) — the same
  operations from the batch `ff.py` CLI.
- [Concepts](concepts.md) — what stuck-at faults, ATPG, and the coverage
  denominator actually mean.
