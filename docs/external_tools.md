# External tools

faultflow is a standalone tool, but it integrates with a small set of external EDA
programs. Some are in the critical path; others are optional or reserved for the
future. Tools are resolved on your `PATH` (a couple have configurable paths).

## In the flow today

### Yosys — synthesis

[Yosys](https://yosyshq.net/yosys/) turns RTL into the flat, standard-cell netlist
faultflow grades. When the input is Verilog, faultflow renders the locked
[synthesis template](user_guide/tcl_shell.md#the-yosys-synthesis-script) and runs
`yosys -s`. One run emits both the JSON netlist (for the core) and a gate-level Verilog
netlist (for verification). Yosys is also used to techmap generic scan cells onto Sky130
scan flip-flops.

Configured with `[design] netlist`, `cell_lib`, `liberty`, and `yosys_ver`.

### CaDiCaL — SAT solving

[CaDiCaL](https://github.com/arminbiere/cadical) is the SAT engine behind the native
ATPG. Unlike the others it is **not** a subprocess — it is linked directly into the C++
core (`-lcadical`). It must be installed at build time; see
[Installation](getting_started/installation.md). Tuned with `[atpg] sat_conflict_limit`
and `sat_timeout_seconds`.

### Icarus Verilog — verification gate

When `[simulation] verify = true`, [Icarus Verilog](https://steveicarus.github.io/iverilog/)
(`iverilog` + `vvp`) compiles the gate-level netlist together with the PDK behavioral
cell models and a generated testbench, then checks the faultflow-generated vectors
against golden outputs. This is an independent correctness gate on the simulation, off by
default. Configured with `verify_tool = iverilog` and `verilog_models`.

### OpenTestability — test-point insertion

OpenTestability is faultflow's test-point insertion partner. There are two integration
paths:

- **Interactive (`add_tp` / `reject_tp`).** In the [Tcl shell](user_guide/tcl_shell.md),
  `add_tp` calls OpenTestability's `analyze_and_add_tp` on the current netlist, re-runs
  ATPG on the resulting test-point-inserted netlist, and prints a Baseline / +TP / Δ
  coverage comparison. Iterations are pushed onto a version stack; `reject_tp` pops the
  last one. Driven by the `[testpoint]` config (`opentest`, `metric`, `threshold`,
  `max_points`).
- **Oracle (`run -c <ot.ofs>`).** faultflow acts as the coverage *oracle* for an
  OpenTestability-driven optimization loop. It reads an OT-format `.ofs`, runs a stuck-at
  ATPG campaign, and writes a flat `oracle_response.json` (coverage, vector count,
  terminal reason, undetected faults) that the caller consumes. See
  [Outputs](user_guide/outputs.md).

The OT-format `.ofs` is distinct from a normal `config.ofs` — it is a small handoff file
naming the post-test-point netlist and where to write the response:

```ini
[input]
netlist = path/to/post_tpi_netlist.json

[design]
top_module = my_core
cell_lib   = cells/sky130/sky130_fd_sc_hd.json

[output]
dir = path/to/output
```

### Quaigh + nl2bench — reference ATPG (optional)

With `[atpg] tool = quaigh`, faultflow converts the gate-level Verilog to BENCH using
`nl2bench`, then runs `quaigh atpg` to produce reference patterns for comparison.
[Quaigh](https://github.com/coloquinte/quaigh) receives BENCH only (never BLIF) and is a
reference/comparison path — the native SAT ATPG remains the default. `nl2bench` may be
present at `venv/bin/nl2bench`.

## Future and planned integrations

```{note}
The items below are **not implemented**. They are recorded here as direction, so the
external-tools story is complete.
```

### autoMBIST — memory built-in self-test

faultflow grades logic (stuck-at and transition) faults; it has **no memory-BIST
capability today**. Integrating an **autoMBIST** flow — generating and grading
memory-BIST structures for embedded RAMs, and combining MBIST with the logic-test
campaign — is a future direction. There is no current dependency on, or integration
with, any MBIST tool.

### OpenSTA — at-speed timing

[OpenSTA](https://github.com/parallaxsw/OpenSTA) is a possible *future* source of real
path delays for at-speed transition testing (replacing the metadata-based timing used
today). It is strictly a timing input — faultflow does **not** use it, and would never
use it, for clock-domain-crossing verification, which is out of scope. A vendored copy of
OpenSTA exists in the source tree but is not wired into the flow.

### Verilator

[Verilator](https://www.veripool.org/verilator/) is mentioned as a possible alternative
verification backend, but only `iverilog` is implemented as a `verify_tool` today.
