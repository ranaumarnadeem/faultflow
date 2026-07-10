# Examples

The repository ships several ready-to-run designs under `examples/` plus the
ISCAS-85/89 benchmark circuits under `tests/benchmarks/`. This page walks through
three representative flows and gives a reusable batch script.

```{note}
The combinational `c17` smoke test is covered in the [Quick start](../getting_started/quickstart.md).
The examples below add live synthesis, scan, and a large real-world core.
```

## Combinational, synthesized from Verilog: `cla4`

`examples/cla4.v` is a 4-bit carry-lookahead adder (9 inputs, 5 outputs, purely
combinational). Its config, `examples/cla4.ofs`, synthesizes the Verilog with Yosys
to the Sky130 HD library — so this example needs `yosys` on your `PATH`.

```bash
python3 ff.py init   --top cla4 -c examples/cla4.ofs
python3 ff.py sim    --top cla4 -c examples/cla4.ofs
python3 ff.py status --top cla4 -c examples/cla4.ofs
```

```{tip}
`examples/cla4.ofs` ships with `[atpg] tool = quaigh`, which needs the external
Quaigh binary. To use faultflow's built-in SAT ATPG instead — no extra tools — set
`tool = native` in the `[atpg]` section.
```

## Sequential with scan: `serial_adder`

`examples/serial_adder.v` is a bit-serial adder with flip-flops. Its config,
`examples/serial_adder_sky130.ofs`, requests four scan chains and the Sky130 techmap.
The flow is: synthesize, insert and check scan, then run scan ATPG.

```bash
python3 ff.py init       --top serial_adder -c examples/serial_adder_sky130.ofs
python3 ff.py scan       --top serial_adder -c examples/serial_adder_sky130.ofs
python3 ff.py scan-check --top serial_adder -c examples/serial_adder_sky130.ofs
python3 ff.py sim --scan --top serial_adder -c examples/serial_adder_sky130.ofs
python3 ff.py status --scan --top serial_adder -c examples/serial_adder_sky130.ofs
```

The combinational and scan campaigns share one database, so `status` (combinational)
and `status --scan` report from the same `output/serial_adder/.faultflow/faultflow.sqlite`.

The same flow can be scripted in the [Tcl shell](tcl_shell.md) with `add_scan`,
`check_scan`, and `run_atpg -scan`.

## More designs under `examples/`

Beyond `cla4` and `serial_adder`, the repository ships additional RTL to grade
with your own config (see [Bringing your own design](#bringing-your-own-design)
for the config pattern — none of these ship with a `.ofs`):

- **DSP filters** — `dsp_iiravg.v` (IIR averager, top `iiravg`), `dsp_boxcar.v`
  (boxcar / moving-average, top `boxcar`), and `dsp_genericfir_small.v` /
  `dsp_genericfir_full.v` (parameterized FIR filters). Real sequential
  arithmetic; good mid-size stuck-at and transition-fault targets.
- **RV32I cores** — `rv32i_single_cycle.sv` and `rv32i_multi_cycle.sv`,
  synthesizable SystemVerilog RISC-V cores (MIT-licensed; see
  `examples/RV32I_UPSTREAM_LICENSE`). Larger multi-module designs that exercise
  the SystemVerilog synthesis path (`read_verilog -sv`).

A pre-synthesized PicoRV32 Sky130 netlist lives under `examples/picorv32_synth/`
and backs the benchmarking numbers (see [Benchmarking](../benchmarking.md)); it
is a generated build artifact and is not tracked in git.

## Bringing your own design

To grade your own RTL, copy a config and point it at your sources. The minimum is a
`[design]` section naming your Verilog and top module, a matched cell library, and
`[atpg] tool = native`:

```ini
[design]
netlist  = path/to/my_design.v
top      = my_design
cell_lib = cells/sky130/sky130_fd_sc_hd.json
liberty  = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib

[atpg]
tool     = native
```

faultflow runs the locked Yosys synthesis script automatically when the input is
Verilog (so `yosys` must be on your `PATH`), after which `init` / `sim` / `status` work
exactly as in the [quick start](../getting_started/quickstart.md). If your netlist is
already synthesized to standard cells, give the Yosys JSON directly and synthesis is
skipped.

## A reusable batch script

The ISCAS-85 circuits (`c17`, `c432`, `c499`) ship as Verilog under
`tests/benchmarks/iscas85/`. This script synthesizes and grades all three with the
native ATPG by generating a small config per circuit. Save it as `run_iscas85.sh`
at the repo root:

```bash
#!/usr/bin/env bash
# run_iscas85.sh — grade the ISCAS-85 circuits with the native SAT ATPG.
set -euo pipefail

for top in c17 c432 c499; do
  cfg="$(mktemp --suffix=.ofs)"
  cat > "$cfg" <<EOF
[design]
netlist  = tests/benchmarks/iscas85/${top}.v
top      = ${top}
cell_lib = cells/sky130/sky130_fd_sc_hd.json
liberty  = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib

[atpg]
tool     = native

[report]
threshold = 100.0
EOF

  echo "=== ${top} ==="
  python3 ff.py init   --top "$top" -c "$cfg"
  python3 ff.py sim    --top "$top" -c "$cfg"
  python3 ff.py status --top "$top" -c "$cfg"
  rm -f "$cfg"
done
```

Run it from a WSL/Linux shell with the venv active:

```bash
chmod +x run_iscas85.sh
./run_iscas85.sh
```

Each circuit's deliverables land in `output/<top>/` (see [Outputs](outputs.md)). The
sequential ISCAS-89 circuits ship as Verilog under `tests/benchmarks/iscas89/` (for
example `s27.v`, `s298.v`), each declaring a module named `s<N>_bench`; point
`[design] netlist` at the `.v` and set `top` to that module name (e.g. `s27_bench`),
then follow the scan flow shown for `serial_adder`.
