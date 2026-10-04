# MBIST insertion

`ff.py mbist-insert` puts memory BIST into your own chip or block RTL, the way a
commercial insertion flow does: for every memory an *insertion file* names, it
generates an [autoMBIST](../external_tools.md) collar, wraps it in a small FaultFlow
*shell*, and swaps it in for the memory instance, keeping your hierarchy. Optionally it
puts the BIST's test ports behind a JTAG TAP and an IJTAG network
([warptap](../external_tools.md)), writes the program that runs every BIST over JTAG,
and, given your `.ofs`, synthesizes the result with its DFT frozen and writes an `.ofs`
ready for the scan and JTAG flow.

```bash
python3 ff.py list-memories --top chip_top --spec mbist.yml
python3 ff.py mbist-insert --top chip_top --spec mbist.yml -c chip.ofs --tap-nonscan
```

Needs autoMBIST (the file's `autombist_cmd`), Yosys on `PATH`, PyYAML for a YAML file,
and warptap with `jtag`.

---

## The insertion file

FaultFlow never guesses which memories to test or how their extra pins are driven:
the file says, and FaultFlow checks it against your design.

```yaml
design:
  sources: [rtl/chip.v, rtl/core.sv]       # read_verilog -sv
  libs: [macros/sram_stubs.v]              # memory macro models or stubs, read -lib
  include_dirs: [rtl/include]
  defines: [SYNTHESIS]
reset: {port: rst_n, active: low}          # the chip reset input
jtag: {tck_max_mhz: 20}                    # or false: the test ports become pins
autombist_cmd: autombist
memory_patterns: ["*sram*"]                # memory macro types list-memories lists
schedule: [[core0_ram], [top_ram]]         # steps run in order; default one per step
memories:
  - name: core0_ram                        # becomes part of port names
    instance: u_core0.u_mem                # as list-memories prints it
    autombist_config: mbist/sram.yml       # autoMBIST's config for this memory
    algo: march-c
    tie: {wmask0: 0xF, csb1: 1, addr1: 0}  # driven by these constants in your RTL
    share_clock: [clk1]                    # on the same net as the memory's clock
    unused_outputs: [dout1]                # read by nothing
```

Paths are relative to the file. A JSON file with the same keys needs nothing extra.

- **`reset`** reaches every collar and shell, and with `jtag` the control TDRs too.
- **`jtag`** needs `tck_max_mhz`, the fastest TCK the chip takes, which only its timing
  knows (it goes in the BSDL); `idcode` (32-bit, bit 0 set) and `bsdl_entity` are
  optional. `jtag: true` is refused.
- **`memories`**: each `instance` is a path through your hierarchy, generate blocks
  included (`g_bank[0].u_mem`). The collar drives the memory's first port; every other
  pin must be declared:
  - `tie`: the pin is driven by exactly that constant in your RTL;
  - `share_clock`: the pin is on the same net as the memory's clock (a plain `assign`
    alias counts, a buffered or gated copy doesn't);
  - `unused_outputs`: nothing reads the pin.

  Each is checked against your RTL before anything is generated, then reproduced
  inside the shell. Any other pin, or a mismatch, is refused. Only dedicated,
  single-port collars are supported.
- **`schedule`**: the BIST program runs one step after another, a step's memories
  together. Sequential (one memory per step) by default, for peak power.

`ff.py list-memories --top T --spec S [--pattern G ...]` lists every memory macro
instance the patterns match: whether the file configures it, each pin with what it
connects to (a constant, "same net as X", unread, or a net), and an entry you can paste.
It never writes the file.

---

## What `mbist-insert` writes

In `--out` (default `mbist_<top>`):

| File | |
|---|---|
| `<top>_mbist.v` | Your design with the shells in, written by Yosys: hierarchy kept, comments, generate blocks and parameters baked in |
| `manifest.json` | Every inserted instance, in autoMBIST's instance-manifest format, with its category (`mbist_shell` for the shell's own logic). With `jtag`, a `test_access` block: the network, the instruction that selects it, the chip reset the TDRs clear on |
| `<top>_mbist.sdc` | Every clock-domain crossing inserted, by hierarchical pin of that RTL |
| `insertion.json` | What was inserted where |
| `<top>_mbist.icl`, `<top>_mbist.bsd` | With `jtag`: the IJTAG network's ICL, and the TAP-only BSDL |
| `<top>_run_mbist.pdl`, `<top>_run_mbist.vec` | With `jtag`: the program that runs every BIST and checks it, as PDL and as TCK and clock vectors |
| `<top>_mbist.ofs`, `synth/` | With `-c`: the synthesized chip, and the `.ofs` for it |

The written RTL is elaborated again before `mbist-insert` returns: Yosys's `check` may
report nothing the original design didn't. A module on the path to a configured memory
that is instantiated more than once gets its own copy, `<module>__mbist_<path>`; no
name FaultFlow creates may collide with a module of your design or a cell of your
liberty.

### The shell

Each memory's shell is the collar plus:

- a reset synchronizer (asserted asynchronously, released on the memory's clock);
- a two-flop synchronizer on each control input (`test_mode`, `bist_start`);
- a two-cycle delay on `bist_done`, so `bist_fail` is final when `done` rises.

The synchronizers and the delay reset from the synchronized reset. Without `jtag`, the
control and status ports reach the top as `<name>_<port>` pins.

### With JTAG

A TAP (`tck`, `tms`, `tdi`, `trst_n`, `tdo`) and an IJTAG network take the ports
instead; the chip gains those five pins and nothing else. A chip that already has one
of them is refused.

- A dedicated **IJTAG_ACCESS** instruction (opcode `1100`) selects the network, so a
  board-level EXTEST can't start a BIST. There is no boundary register: EXTEST, SAMPLE
  and PRELOAD select BYPASS, and the BSDL says so in its `DESIGN_WARNING`.
- Each control port is a WRITE TDR that also clears on the chip reset, through a
  two-flop TCK synchronizer: `test_mode` can't come up 1 without TRST. If TCK is
  parked after a chip reset, the TDRs stay cleared, by design.
- Each status port is a READ TDR that captures through two TCK flops.

The BIST program writes `test_mode`, then `bist_start` in a later Update-DR, runs each
step's clock for the step's longest BIST, reads `done`, then `fail` in a later capture,
and releases the controls. Every latency the inserted logic adds is a named term,
never absorbed by margin: two clock cycles each for the reset release, the control
synchronizer and the done delay, and two TCK edges after a run before a status capture
and after the chip reset before the first Update-DR. The BIST length is autoMBIST's own
bound. warptap retargets one instrument per scan, so a step's memories start a few TCK
scans apart.

---

## Synthesis and the scan flow

With `-c chip.ofs`, `mbist-insert` synthesizes the result with that `.ofs`'s liberty.
The DFT is never optimized together with your logic: each controller and repair block,
then each shell, then the TAP and the network, is synthesized alone and spliced in.

It then writes `<top>_mbist.ofs`: your `.ofs`, every section kept, with:

- `[design] netlist`: the synthesized chip, and `output_root`: `<out>/output`, so the
  inserted chip, whose top has the original's name, keeps its own outputs;
- `[blackbox] instances`: your blackboxes, less the memories now inside shells, plus the
  chip's memories;
- `[clocks] ports`: the scan clocks;
- `[scan]`: your settings, at least one chain per clock domain, each shell's reset
  synchronizer in `nonscan_cells`, and the chip reset held inactive;
- `[autombist] manifest`: the chip's manifest.

`--tap-nonscan` also runs the TAP and the network non-scan, `trst_n` and `tck` held at
0, for `ff.py jtag` to test. An `.ofs` that holds the chip reset active, or names it a
scan port or a clock, is refused before anything is inserted.

Then the usual flow runs from that `.ofs`:

```bash
python3 ff.py init --top chip_top -c mbist_chip_top/chip_top_mbist.ofs
python3 ff.py scan --top chip_top -c mbist_chip_top/chip_top_mbist.ofs
python3 ff.py scan-check --top chip_top -c mbist_chip_top/chip_top_mbist.ofs
python3 ff.py sim --scan --top chip_top -c mbist_chip_top/chip_top_mbist.ofs
python3 ff.py jtag --top chip_top -c mbist_chip_top/chip_top_mbist.ofs
```

- Each shell's reset synchronizer stays out of scan and **settles**: with the chip reset
  held inactive, it reads 1 two clock pulses in. Scan ties it there, and every pattern
  starts with a two-pulse *preamble* (`preamble_cycles` in exported patterns). Its own
  faults are reset faults (`excluded_reset`). See `[scan] nonscan_cells` in
  [Configuration](configuration.md).
- The control synchronizers and the done delay are ordinary scan flops. A control TDR
  bit is held at its reset value in scan, so its branch into the synchronizer stuck at
  0 is `hold_unresolved`: only freeing the hold would show it, and a BIST run over
  JTAG does.
- `ff.py jtag` builds its program for IJTAG_ACCESS, reads the manifest's IDCODE, and
  pulses the chip reset during the TRST lead-in, then holds it inactive (held active,
  the control TDRs would stay at 0). Its X-isolation proof follows each reset through
  the synchronizers to the collars. See [Running without the shell](running_without_shell.md).
- `ff.py scan` writes `<top>_scan.sdc` beside `<top>_scan.v`: the inserted crossings by
  pin of the scanned netlist. `scan-compress` and `scan-compact` write
  `<top>_compressed.sdc` and `<top>_compacted.sdc` beside `<top>_compressed.v` and
  `<top>_compacted.v`, where the chip is `core_inst` (with both, the compacted
  netlist is the whole chip).
  With a TAP, the `.ofs` names the compression and compaction channels `comp_si` and
  `comp_so`, away from the TAP's `tdi` and `tdo`.
- The coverage report breaks coverage down by category, `mbist_shell` included
  ([Outputs](outputs.md)).

---

## Limits

- The RTL is written by Yosys: hierarchy is kept, but comments, generate blocks and
  parameters are baked in.
- Collars are dedicated (one memory each) and single-port.
- One TAP; a chip that already has one is refused. The BSDL is TAP-only and
  non-conformant until a boundary register exists.
- A chip reset during a JTAG session clears the MBIST controls, by design.
- Timing sign-off of the inserted synchronizers is yours; the SDCs say what was
  inserted, they are not a CDC analysis.
- `share_clock` means the same net as the memory's clock: a clock reaching it through a
  buffer or a gate is refused.
- No credit is given for the BIST run itself: the memories are blackboxes, unknown to
  the two-valued simulator.
