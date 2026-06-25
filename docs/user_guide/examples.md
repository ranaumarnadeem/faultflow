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

## A real core, pre-synthesized: PicoRV32

`examples/picorv32a.v` is the full [PicoRV32](https://github.com/YosysHQ/picorv32)
RV32I core. To avoid a multi-minute synthesis on every run, a pre-synthesized Sky130
netlist is shipped under `examples/picorv32_synth/`, and `examples/picorv32a_sky130.ofs`
consumes the JSON directly:

```bash
python3 ff.py init   --top picorv32a -c examples/picorv32a_sky130.ofs
python3 ff.py sim    --top picorv32a -c examples/picorv32a_sky130.ofs
python3 ff.py status --top picorv32a -c examples/picorv32a_sky130.ofs
```

This config is a good template for large designs: it sets
`unsupported_cells = blackbox` (to tolerate Yosys internal cells such as
`$scopeinfo`), `tie_xz = true`, a lower `max_rounds = 5`, and a `threshold = 90.0` so
the run is bounded.

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
skipped — as the PicoRV32 example does.

## A reusable batch script

The ISCAS-85 circuits (`c17`, `c432`, `c499`) ship as Sky130-mapped JSON under
`tests/benchmarks/iscas85/synth_sky130/` (OSU035 under `…/synth/`). This script grades all three
with the native ATPG
by generating a small config per circuit. Save it as `run_iscas85.sh` at the repo
root:

```bash
#!/usr/bin/env bash
# run_iscas85.sh — grade the ISCAS-85 circuits with the native SAT ATPG.
set -euo pipefail

for top in c17 c432 c499; do
  cfg="$(mktemp --suffix=.ofs)"
  cat > "$cfg" <<EOF
[design]
netlist  = tests/benchmarks/iscas85/synth_sky130/${top}.json
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
sequential ISCAS-89 netlists ship as `*_bench.json` under
`tests/benchmarks/iscas89/synth_sky130/` (for example `s27_bench.json`,
`s298_bench.json`); point `[design] netlist` at the file and set `top` to the module
name, then follow the scan flow shown for `serial_adder`.
