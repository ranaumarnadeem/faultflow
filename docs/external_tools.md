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
  `max_points`). See [Test-point insertion](user_guide/testpoints.md) for the full
  workflow, including the version-stack format and PDK TPI cell support.
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

### autoMBIST — memory built-in self-test logic (optional)

autoMBIST generates memory-BIST wrapper RTL for SRAM macros. faultflow grades the logic
it adds — the MBIST controller, self-repair, diagnosis and repair-remap logic — with a
scan test, the memory itself staying a blackbox. faultflow only runs autoMBIST as a
subprocess and reads the files it writes.

```bash
python3 ff.py autombist-generate --config mbist.yml --out build \
    --liberty cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib \
    --cell-lib cells/sky130/sky130_fd_sc_hd.json
```

This runs `autombist generate --emit-manifest` (`--autombist-cmd` names another command,
e.g. `'python3 -m autombist'`), then builds the design from the manifest: every distinct
instrument synthesized once on its own, the wrapper synthesized with the instruments and
the memory as blackboxes, and the instruments' netlists spliced back in. It writes the
composed netlist and a `.ofs` for it: `[blackbox] instances` names the memories, and
`[autombist] manifest` names the manifest. The Tcl shell's `autombist_generate` does the
same and loads the design into the session.

A memory's outputs are unknown to a scan test: every scan-flop capture and primary output
they reach is masked (`[blackbox] output_value`), and a fault only a memory pin could
reveal counts as `blackbox_unresolved`.

**JTAG access (`--test-access`, `-test_access` in the shell).** autoMBIST then also runs
`wrap-test-access --manifest <dir> --emit-icl`, which puts the design's control and
status ports behind a JTAG TAP and an IJTAG network (one SIB per port, one TDR bit per
port bit; it needs the [warptap](https://github.com/ranaumarnadeem/warptap) package). The
control ports (`test_mode`, `bist_start`, ...) become JTAG-only: their pins are left
without fanout. The status ports stay readable at their pins. faultflow builds the wrapped
design from the manifest's `test_access` block alone: each distinct module of the wrapped
netlist is synthesized once — including the parameter-specialized
`$paramod$<hash>\march_c_top`, used verbatim — and spliced into every instance of it.
The glue's own non-library cells must be exactly the listed instances.

The TAP and its network run on `tck`/`trst_n`, the MBIST logic on `clk`/`rst_n`: two
clock domains. The `.ofs` declares both in `[clocks]` and asks for one scan chain per
domain; scan insertion scans every flop of both, and holds `trst_n` inactive like
`rst_n`.

**Coverage by category.** With `[autombist] manifest` set, the coverage report breaks
detected faults, the denominator and `blackbox_unresolved` down by the manifest's
instance categories (`autombist_categories` in `coverage_report.json`, a table in
`coverage.rpt`): memory, mbist_controller, self_repair, diagnosis, repair_remap and, for a
wrapped design, jtag_tap, ijtag_sib, ijtag_tdr, ijtag_scan_mux. A fault belongs to the
instance of the cell it sits on; what no instance owns — the wrapper's own logic, its
ports, constant nets — counts as `glue`, so the categories add up to the totals.

**The TAP and the IJTAG network over JTAG.** `ff.py jtag` (the shell's `run_jtag`)
plays the network-integrity program a production flow uses -- IDCODE, the instruction
register's capture and length, BYPASS and every unimplemented opcode, every TAP state
transition with each TAP register held in Pause at 0 and at 1, each SIB opened alone, the
network held still under SAMPLE/PRELOAD and through Pause, each control TDR written and
read back, a TMS reset -- through the TAP of the scanned netlist, grades the scan
campaign's faults at TDO, and reports the credit beside the scan coverage (`jtag` and
`combined`, per category too). The program is built by warptap's
`build_integrity_program` from the network the manifest describes (it needs warptap with
`warptap.tap_integrity`), or read from a `warptap-tck-program` file. What it leaves
undetected in the TAP is mostly beyond any TCK sequence: a register's behaviour under
another instruction (every read starts with a capture), decodes of opcodes the TAP never
latches, and TDO outside the shift states.

```{admonition} Known limitation: TAP non-scan operation
:class: warning

faultflow scans the TAP and the IJTAG network's flops like any other logic, so scan
patterns grade their faults too, and `ff.py jtag` adds JTAG credit on top. Running the
TAP non-scan, with only the JTAG program testing it, is not modeled yet. The
`coverage.rpt` of a wrapped design notes this.

The IJTAG network is the data register of EXTEST: its top-level SIBs are selected by a
decode of the TAP's instruction, so reading or writing it needs EXTEST loaded, and
Test-Logic-Reset (five TMS=1 cycles) deselects it. warptap 0.0.2 and earlier tie that
select high instead, so their network moves on every DR scan, whatever the instruction
(two all-ones scans under IDCODE start the BIST); re-wrap a design built with them.
```

## Future and planned integrations

```{note}
The items below are **not implemented**. They are recorded here as direction, so the
external-tools story is complete.
```

### OpenSTA — at-speed timing

[OpenSTA](https://github.com/parallaxsw/OpenSTA) is a possible *future* source of real
path delays for at-speed transition testing (replacing the metadata-based timing used
today). It is strictly a timing input — faultflow does **not** use it, and would never
use it, for clock-domain-crossing verification, which is out of scope. A vendored copy of
OpenSTA exists in the source tree but is not wired into the flow.

### Verilator

[Verilator](https://www.veripool.org/verilator/) is mentioned as a possible alternative
verification backend, but only `iverilog` is implemented as a `verify_tool` today.
