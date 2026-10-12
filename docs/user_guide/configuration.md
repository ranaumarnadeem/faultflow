# Configuration reference

faultflow is configured with an **INI-style** file conventionally named
`config.ofs` and passed with `-c`. It is parsed with Python's `configparser`. The
top module always comes from `--top` on the command line, not from the file.

Booleans accept `true/false`, `1/0`, `yes/no`, or `on/off`.

The canonical, fully-commented template is
[`config.ofs.example`](https://github.com/ranaumarnadeem/faultflow/blob/main/config.ofs.example).
Copy it and edit:

```bash
cp config.ofs.example config.ofs
```

```{note}
The OpenTestability **oracle** mode (`faultflow run -c ot.ofs`) reads a small,
differently-shaped handoff file, not this schema — see "OpenTestability — test-
point insertion" under [External tools](../external_tools.md).
```

## A complete example

```ini
[design]
netlist   = tests/benchmarks/iscas85/synth_sky130/c17.json
top       = c17
cell_lib  = cells/sky130/sky130_fd_sc_hd.json
liberty   = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib
yosys_ver = 0.61+129

[fault_model]
model                = stuck_at
launch               = loc
collapsing           = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
verify            = false
verify_tool       = iverilog
verilog_models    = cells/sky130/sky130_fd_sc_hd.v

[atpg]
tool                = native
mode                = comb
output              = patterns.test
random_vectors      = 64
sat_conflict_limit  = 100000
max_rounds          = 20
sat_timeout_seconds = 10
compaction          = reverse

[report]
output    = coverage.rpt
threshold = 95.0

[scan]
chains      = 1
scan_in     = scan_in
scan_out    = scan_out
scan_enable = scan_en
run_techmap = true
```

## `[design]`

| Key | Default | Meaning |
|---|---|---|
| `netlist` | `design.json` | Input Yosys JSON or Verilog netlist |
| `cell_lib` | `cells/sky130/sky130_fd_sc_hd.json` | JSON cell map consumed by the C++ core |
| `liberty` | Sky130 HD `.lib` | Liberty file — used by **Yosys only** |
| `verilog_models` | — | Behavioral cell models for the iverilog gate (falls back to `[simulation] verilog_models`) |
| `yosys_ver` | `""` | Yosys version string, recorded in the fingerprint |
| `output_root` | `output` | Where the campaign's outputs go: `<output_root>/<top>`. The `.ofs` `mbist-insert -c` writes sets its own, so the inserted chip (same top) doesn't share the original's |

```{note}
The cell map and Liberty must be a matched pair for the same PDK. The two shipped pairs
are: **Sky130 HD** — `cell_lib = cells/sky130/sky130_fd_sc_hd.json`,
`liberty = cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib`; and **OSU035** —
`cell_lib = cells/osu/osu035.json`, `liberty = cells/osu/osu035_stdcells.lib`.
```

## `[fault_model]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `model` (alias `type`) | `stuck_at`, `transition` | `stuck_at` | Fault model. `model` is preferred; `type` is a back-compat alias |
| `launch` | `loc`, `los` | `loc` | Transition launch style for **scan** transition ATPG. `los` (launch-on-shift) is supported for the scan flow only; combinational broadside transition rejects `los` |
| `collapsing` | bool | `true` (see note) | Enable equivalence-only fault collapsing — never dominance, so coverage is provably unchanged (see [Collapsing rules](../collapsing_rules.md)). `transition` + `collapsing=true` is an error |
| `include_clock_faults` | bool | `false` | Count clock-net faults in the denominator |
| `include_reset_faults` | bool | `false` | Grade async reset/set-tree faults via implication instead of excluding them. In a scan test a pattern may then set a reset input active, in the capture only: the input stays inactive while the chains shift (`shift_pi_values` in exported patterns). Fingerprinted — toggling it forces a fresh campaign |

```{important}
`collapsing` defaults to **true** (on) when the key is omitted entirely.
`config.ofs.example` and every shipped example config set it to `false`
explicitly, so out-of-the-box runs have collapsing **off** — but deleting that
line rather than setting it turns collapsing back on.
```

## `[simulation]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `unsupported_cells` | `fail`, `blackbox` | `fail` | Policy when a cell is not in the cell map (see below) |
| `verify` | bool | `false` | Run the optional iverilog verification gate |
| `verify_tool` | `iverilog` | `iverilog` | Verification backend |
| `verify_use_power_pins` | bool | `false` | Drive `VPWR=1`/`VGND=0` and pass `-DUSE_POWER_PINS` in the generated testbench, for behavioral models that need it |
| `sim_threads` | int | `1` | Threads for parallel fault grading, the dominant ATPG cost. `1` = serial, `0` = auto (`cpu_count - 2`, min 1), `N` = `N` threads. Coverage is bit-identical for any value — only wall-clock changes |
| `verilog_models` | — | Sky130 models | Behavioral cell models for verification |

```{admonition} Unsupported-cell policy
:class: important
With `fail` (the default), faultflow aborts immediately if the netlist contains any
cell not in the cell map — coverage is never reported on an unknown netlist. With
`blackbox`, unknown cells are explicitly modeled as test boundaries: their outputs
get no fault sites and are excluded from the denominator (and reported separately).
There is no silent-skip option.
```

## `[atpg]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `tool` | `native`, `sat_atpg`, `quaigh` | `native` | ATPG backend. `native` is the built-in SAT ATPG |
| `mode` | `comb` | `comb` | Only combinational mode is supported |
| `output` | path | `patterns.test` | Fallback pattern file name (under `output/<top>/`). Not produced by a normal `sim`/`run_atpg` run — see [Outputs & reports](outputs.md) |
| `random_vectors` | int | `64` | Random vectors applied before targeted ATPG |
| `random_stop_coverage` | float | `85.0` | Stop grading further random vectors and switch to SAT once coverage reaches this percent — or once the full `random_vectors` budget is graded, whichever comes first. `0.0` disables the early switch |
| `sat_conflict_limit` | int | `100000` | Per-fault CaDiCaL conflict limit |
| `max_rounds` | int | `20` | Maximum progressive ATPG rounds |
| `sat_timeout_seconds` | int | `10` | Per-fault SAT timeout; the fallback used when `sat_timeout_schedule` is empty |
| `sat_timeout_schedule` | comma list | `2,10,60` | Escalating per-fault SAT timeout, smallest tier first — a fault only escalates to the next tier when it times out at the current one. Empty uses the fixed `sat_timeout_seconds` for every fault |
| `compaction` | `none`, `reverse`, `dynamic` | `reverse` | Test-set compaction after ATPG. `reverse` is reverse-order, fault-sim-verified static compaction (coverage-preserving); `dynamic` is sim-verified cube-packing |
| `fault_drop_sat` | bool | `true` | Fault-simulate an accepted SAT vector against *all* remaining faults, not just the target fault, so incidental "fallout" detections are dropped too |
| `cone_restrict` | bool | `true` | Restrict each fault's CNF to its structural cone of influence |
| `incremental_sat` | bool | `false` | Use incremental-SAT (IFC) solving, one observable cone at a time |
| `pack_orders` | int | `1` | Number of packing orders tried during dynamic compaction |
| `order_by_cone_size` | bool | `true` | Order faults by ascending structural cone size so the fastest SAT calls run first, incidentally detecting more faults early. Coverage-identical; stuck-at only |
| `workers` | int | `1` | Parallel SAT worker processes (`1` = serial). Requires a fork-capable OS (Linux/WSL); silently falls back to serial on Windows/spawn platforms. Also settable interactively in the shell with `WORKERS N` |
| `preflight` | bool | `true` | Run OpenTestability structural reconvergence analysis before ATPG (reconvergent-site faults sorted last and skip the short timeout tier). Ordering only: a fault is classified redundant only when SAT proves it UNSAT. Requires the `opentest` binary on `PATH`; falls back silently when unavailable |
| `preflight_tech` | str | `""` (auto-detect) | PDK tech tag passed to `opentest`'s preflight pass. Empty auto-detects from the `cell_lib` path (`sky130` unless the path contains `osu035`/`osu`) |
| `easy_fault_reserve` | int | `2` | Easy/hard worker split, applied only when `workers >= 4`: each wave reserves this many slots for easy (small-cone, non-reconvergent) faults while the rest run hard faults, so fast and slow faults progress concurrently. `0` disables the split |

## `[report]`

| Key | Default | Meaning |
|---|---|---|
| `output` | `coverage.rpt` | Human-readable report file name |
| `threshold` | `95.0` | Target coverage percent at which ATPG stops |

## `[scan]`

| Key | Default | Meaning |
|---|---|---|
| `chains` | `1` | Number of scan chains. A chain never spans two clock domains, so a multi-clock design needs one per domain at least; flip-flops split as evenly as the count allows, and where that would straddle a domain boundary, each domain gets whole chains in proportion to its flip-flop count |
| `max_chain_length` | (none) | Maximum flip-flops per chain; empty means unbounded. With `chains = 1`, the chain count becomes what keeps every chain within it, counted per clock domain |
| `wrapper_chains` | (none) | Chains for the flops of an IEEE 1500 wrapper (`[wrap] enabled`). They get chains of their own after the core's, balanced, in the wrapper's ring order (input bits, then output bits, as the netlist lists the ports), with scan ports `wbr_si`/`wbr_so` (`wbr_si_0`, `wbr_so_0`, ... with several) and the one scan enable. Empty means as many as keep each no longer than the longest core chain; for a block without flops of its own, `max_chain_length`, else one chain |
| `scan_in` | `scan_in` | Scan input port base name |
| `scan_out` | `scan_out` | Scan output port base name |
| `scan_enable` | `scan_en` | Scan enable port name |
| `run_techmap` | `true` | Run the Sky130 scan-cell techmap after stitching |
| `nonscan_cells` | (none) | Instance-name globs, e.g. `u_tap__*, u_sib_*`: flip-flops left out of scan on purpose, like a JTAG TAP and its IJTAG network that `ff.py jtag` tests through TCK. Each must hold still for the whole scan test: either a `hold` input keeps its clear or preset active, and the scan view ties its output at the value that forces, or the `test_setup` program loads it and it holds that value through the test (its clock held, its enable held off, or its D tracing back to that value, its own output through a mux a held input selects, say: a *programmed* flop, tied at that value), or its clock is held and nothing free can reset it, and its output is unknown, masked like an unknown blackbox output, or it *settles*: a scan clock clocks it, the holds keep its clear and preset inactive, and its D traces to holds, constants and other settled flops (a reset synchronizer with `rst_n` held inactive). A settled flop is tied at the value it settles to, every pattern starts with the pulses that settle it (`preamble_cycles`), and its own faults are `excluded_reset`. Anything else is an error. The other non-scan flops' faults, and those only they see, are `excluded_jtag`; a glob selects settled flops or others, not both |
| `hold` | (none) | `input:0\|1` list, e.g. `trst_n:0, tck:0`: inputs held at that value in every scan pattern, e.g. to keep a non-scan TAP in reset. Not a scan port or scan clock, and not a value that clears or presets scan flops. A fault only the held values block is `hold_unresolved` |
| `test_setup` | (none) | A test setup program (JSON, see below): the cycles a tester applies before every scan pattern to put the chip in its test mode, a test-mode register loaded through ports of its own, say. Each cycle sets every input (0 if it doesn't name it) and may pulse scan clocks; FaultFlow grades every pattern after it, fault-free, and the non-scan flops it leaves at a value they hold are *programmed* (`nonscan_cells`). A stuck-at that would change what it loads into one is `excluded_jtag`, like those that would let one go during the test. Not with scan compression or compaction |
| `scan_test_access` | `false` | The test instruments' flops (a JTAG TAP, the IJTAG network: a flop clocked from `[jtag] tck`, an instance of a JTAG category of the `[autombist]` manifest, a `warptap_*` cell) are scan flops on purpose. They are tested through TCK by `ff.py jtag`, so without this key `ff.py scan` and `scan-check` refuse one on a scan chain: keep them out with `nonscan_cells` and `hold`. Set by `--scan-tap` / `-scan_tap`; `ff.py scan` then records them in the scan manifest (`test_access_scanned`) |
| `shift_controls` | `fail` | What `scan-check` does with a scan flop whose asynchronous clear or preset isn't held inactive while the chains shift: fail (`fail`), or pass with a warning (`warn`), for resets you have checked yourself. On a real chip a scan flop's clear and preset act during shift too, but FaultFlow's simulation turns them off, so that flop's patterns wouldn't unload what FaultFlow expects. Held inactive means traced through buffers, inverters, gates a held input sets, muxes a held select steers and any other cell whose held inputs fix its output, to constants, held inputs and settled, forced or programmed `nonscan_cells` flops. The held inputs are scan enable (at 1), `hold`, and any input that reaches a scan flop's clear or preset through buffers and inverters alone, which the scan test holds inactive by itself (`rst_n`, or `rst` through an inverter). A scanned reset synchronizer, or a reset from an input the patterns set, isn't held: list the synchronizer's flops in `nonscan_cells`, hold the input, or gate the reset in the RTL with scan enable or a held test-mode input |

## `[testpoint]`

Used by the `add_tp` command in the [Tcl shell](tcl_shell.md) to drive
OpenTestability.

| Key | Default | Meaning |
|---|---|---|
| `opentest` | `opentest` | Path to the `opentest` binary |
| `metric` | `scoap` | Testability metric |
| `threshold` | `50` | Target threshold passed to OpenTestability |
| `max_points` | `10` | Maximum test points to insert |

## `[clocks]`

Declare multiple clock domains (also settable interactively with `add_clock`, or
one-shot from the CLI with `add-clock`).

| Key | Format | Meaning |
|---|---|---|
| `ports` | comma list, e.g. `clk_a, clk_b` | Clock port names |
| `off` | comma list of `port:0\|1` | Per-port inactive level |
| `groups` | `;`-separated groups, e.g. `clk wrck; clk_b clk_c` | Scan clocks a tester pulses together, at speed: one domain to a transition test, so a path from one to another is tested rather than `excluded_cross_domain`. FaultFlow pulses every scan clock in each capture; a group says the chip's clocks really are synchronous. Each must be a scan clock |

### The test setup program (`[scan] test_setup`)

```json
{"format": "faultflow_test_setup_v1",
 "cycles": [{"inputs": {"cfg_rst_n": 0}},
            {"inputs": {"cfg_rst_n": 1, "cfg_en": 1, "cfg_in": 1},
             "pulse": ["clk"]}]}
```

Each cycle sets every input of the scanned chip to the value it gives it (an input is
named as a pattern names it: the port, or `port[i]` for a bus bit), 0 if it doesn't
name it -- `[scan] hold` doesn't apply in it. The scan clocks stay low but those in
`pulse`, which rise at the cycle's end and fall again with the same inputs, so a
falling-edge flop takes the cycle's inputs, as on a tester. A clock that isn't a scan
clock (a TCK) is an input like any other, set as a level. The file is checked when the
config loads.

FaultFlow runs it before every pattern it grades, with no fault active and nothing
compared, and finds what it leaves in each `nonscan_cells` flop from any power-up state
(three-valued: a flop it doesn't set is unknown). A flop it leaves at a value that then
holds through the test is tied there in the scan view. Every exported pattern carries
the program (`setup`); `write-patterns` writes it as the `test_setup` macro, given
before each pattern, or once, first, when the loads overlap the unloads.

## `[blackbox]`

| Key | Format | Meaning |
|---|---|---|
| `instances` | comma list, e.g. `u_sram, u_pll` | Instances modeled as test boundaries (pseudo-PI/PO). Scan runs model them opaque instead: a scan test can neither set, know nor observe them, so their inputs are unobserved and their outputs hold `output_value` |
| `output_value` | `x` or `0`, plus per-instance `inst:x` / `inst:0` overrides, e.g. `x, u_rom:0` | Scan runs only: what an instance's outputs hold during the scan test. `x` (the default) is unknown, like an SRAM's output, which holds whatever it last read: every scan flop capture and primary output it reaches combinationally (for launch-on-capture, also through a flop that captured it at launch) gets no detection credit and is don't-care in exported patterns. Faults seen only there count as `blackbox_unresolved`. `0` asserts the design holds the output at 0 in test mode. An unknown output that reaches a scan flop's clock, scan-in, scan-enable or async set/reset is an error |

## `[testmode]`

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `mode` | `functional`, `intest`, `extest` | `functional` | IEEE 1500 wrapper test mode of a scan test (`sim --scan`); `intest` and `extest` need the wrapper (`[wrap] enabled`) |

## `[wrap]`

The IEEE 1500 wrapper `ff.py wrap` puts on the design's ports (see
[Running without the shell](running_without_shell.md)).

| Key | Allowed values | Default | Meaning |
|---|---|---|---|
| `enabled` | `true`, `false` | `false` | `ff.py scan` wraps the design first, then scans it: the wrapper's flops go on wrapper chains (`[scan] wrapper_chains`) and the scan manifest records the wrapper. A scan test holds the mode pins as its mode says: both at 0 for `sim --scan` (functional), INTEST at 1 and EXTEST at 0 for `intest`, the reverse for `extest`; a `[scan] hold` on a mode pin that disagrees is an error |
| `clock` | a port name | the design's one clock | The port the wrapper's flops run on. A design with several clocks must name one; a block without flops gets it as a new input |
| `intest_pin` | a port name | `wbr_intest` | The INTEST mode pin the wrapper adds |
| `extest_pin` | a port name | `wbr_extest` | The EXTEST mode pin the wrapper adds |
| `exclude` | comma list of globs on `port` or `port[bit]` | empty | Port bits left unwrapped; `*` and `?` are wildcards, brackets match themselves (`b[1]`, `b[*]`) |
| `control` | `pins`, `wsp` | `pins` | How the mode is set: the two mode pins, or an IEEE 1500 wrapper serial port's instruction (below) |

`wbr_model` went with the abstract `$wbc_*` wrapper; a config that sets it is
refused.

### The wrapper serial port (`control = wsp`)

In place of the mode pins, the wrapper gets an IEEE 1500 wrapper serial port --
`wrck`, `wrstn`, `selectwir`, `shiftwr`, `capturewr`, `updatewr` and `wsi` in, `wso`
out -- and, as sky130 cells:

- the instruction register (WIR): a 3-bit shift stage on `wrck` (`wsi` into bit 0,
  shifting while `selectwir` and `shiftwr` are 1), and an update stage, the
  instruction in force, which takes it on the falling edge of `wrck` while
  `selectwir` and `updatewr` are 1, and which `wrstn` at 0 clears to WS_BYPASS at once;
- the bypass register (WBY): one bit from `wsi` to `wso`, shifting while `shiftwr` is
  1 and capturing 0 while `capturewr` is 1, the WIR deselected; `wrstn` clears it;
- the decode of the instruction onto the boundary cells' controls.

| Instruction | Code | The wrapper |
|---|---|---|
| WS_BYPASS | 000 (after `wrstn`) | functional; `wso` is WBY |
| WS_EXTEST | 001 | EXTEST, the boundary register shifted from `wsi` to `wso` |
| WP_INTEST | 010 | INTEST, the wrapper chains shifted on their own ports with the scan enable |
| WP_EXTEST | 011 | EXTEST, likewise |
| WS_INTEST | 100 | INTEST, the boundary register shifted from `wsi` to `wso` |
| any other | | as WS_BYPASS |

In a serial instruction (WS_*) the wrapper chains join into one boundary register,
`wsi` into the first chain's head and the last chain's tail out to `wso`, shifting
while `shiftwr` is 1 with the WIR deselected; every boundary cell holds its value but
while `capturewr` is 1 with the WIR deselected, when the cells the mode observes
capture -- so they keep their values while the WIR shifts and updates. The boundary
cells' flops run on `wrck`, so `[wrap] clock` stays empty. The instruction and bypass
registers' flops stay out of the scan chains (reason `wsp`).

`ff.py wrap`, `ff.py scan` and `scan-check` take such a wrapper; scan-check follows
the chains as a parallel instruction (or WS_BYPASS) leaves them.

#### Scan tests through the WIR

A scan test of such a wrapper puts its instruction in the WIR first: every pattern
starts with a test setup program -- WS_BYPASS for `sim --scan`, WP_INTEST for
`intest`, WP_EXTEST for `extest` -- that clears the WSP (`wrstn` at 0), shifts the
instruction in (bit 2 first, a `wrck` pulse each) and updates it (one more pulse).
The test then holds `wrstn` at 1 and `selectwir`, `shiftwr`, `capturewr`,
`updatewr` and `wsi` at 0, so the WIR and WBY keep their values while `wrck` pulses
with the scan clock; a `[scan] hold` that disagrees is an error, and so is a
`[scan] test_setup` of its own. The exported patterns carry the program (`setup`),
the tester program applies it, and the STIL writes it as the `test_setup` macro.
The boundary cells run on `wrck`, the core on its own clock: for transition faults
on the paths between them, group the two (`[clocks] groups = clk wrck`).

Such a test can't test the WSP itself, its instruction programmed and held: it
leaves the WSP's faults -- its ports', its registers', its decode's and its serial
path's -- to the WSP's own test (`excluded_wsp`). Where the decode's nets enter the
boundary cells, the faults are the cells', owned by the mode that activates them,
as the mode pins' are.

In a project (`ff.py project`), blocks wrapped so share one WSP on the SoC: the glue
brings each of `wrck`, `wrstn`, `selectwir`, `shiftwr`, `capturewr` and `updatewr`
to every block from one SoC input, through buffers and inverters (`wrck` through
buffers alone), and chains their `wsi` and `wso` through buffers, from a SoC input
through every block to a SoC output. The SoC's program loads the mode's instruction
into every block's WIR along that chain, and a block's retargeted INTEST patterns
start with it. A project's blocks are wrapped all one way.

## `[autombist]`

Written by `ff.py autombist-generate` (see [External tools](../external_tools.md)).

| Key | Format | Meaning |
|---|---|---|
| `manifest` | path | The autoMBIST instance manifest the netlist was built from. The coverage report then breaks detected faults, the denominator, `blackbox_unresolved` and `hold_unresolved` down by the manifest's instance categories. Report-only: not part of the campaign fingerprint |

## `[compression]` and `[compaction]`

Scan test-pattern compression (`ff.py scan-compress`: a ring-generator and
phase-shifter decompressor in front of the scan inputs) and response compaction
(`ff.py scan-compact`: an XOR-tree space compactor behind the scan outputs). Each
wraps the scanned design, which becomes the instance `core_inst`, in
`<top>_compressed.json` or `<top>_compacted.json`, and in `<top>_compressed.v` or
`<top>_compacted.v`, the same as sky130 Verilog (the design's scan cells mapped as
`ff.py scan` maps them; not with `[scan] run_techmap = false`). With both, the
compacted netlist holds the decompressor too: it is the whole chip, the design still
`core_inst`. `scan-compact` builds it around the decompressor `scan-compress`
inserted, and `scan-compress` run afterwards builds it again around its new one.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Insert it |
| `channels` | `8` | External channels. Compression: an LFSR width, 8, 16, 32 or 64 |
| `scan_enable`, `clock` | the design's | `[compression]` only: the scan enable and clock the decompressor uses |
| `channel_port` | `tdi` / `tdo` | The name of the channel bus. It can't be a port the design already has, and `ff.py jtag` refuses a TAP pin's name. The `.ofs` `mbist-insert -c` writes for a chip with a TAP sets `comp_si` and `comp_so` unless yours sets them |

The decompressor loads every scan cell from one seed of `channels` bits per
pattern (a channel value at the start of the load, then the ring generator runs),
so a compressed design can apply only the loads that seed space spans. `sim --scan`
generates only those: SAT solves with each scan cell tied to its seed bits
(launching on shift, each chain's launch-shift bit too), random fill draws random
seeds, and every pattern is graded and exported as the seed's whole scan-in stream
(`load_care` names every position). A fault some test detects but no load the
decompressor makes is `compression_unresolved`: undetected, in the denominator,
never redundant. Only more channels (a wider seed) widen what it can load; how the
cells are split into chains doesn't. A campaign graded before this can't be resumed:
re-run with `--clean`.

## `[jtag]`

Used by `ff.py jtag` / `run_jtag` (see [Running without the shell](running_without_shell.md)).
Every key is optional; the defaults fit a TAP warptap inserted. Report-only: not part of the
campaign fingerprint (a JTAG grade keys itself to its program, netlist and holds).

| Key | Format | Default | Meaning |
|---|---|---|---|
| `tck`, `tms`, `tdi`, `trst_n`, `tdo` | port names | the same | The TAP's ports |
| `hold` | `input:0\|1` list, e.g. `rst_n:0, test_mode:0` | — | What to hold other inputs at, over the defaults (0, or the level that keeps a flop's reset active) |
| `program` | path | — | A `warptap-tck-program` JSON file to play; without it, one is built from the `[autombist] manifest` with warptap |
| `ir_width` | integer ≥ 2 | `4` | The TAP's instruction-register width, for a built program |
| `idcode` | 32-bit integer (bit 0 set), or `none` | `0x1A5A5003` | The TAP's IDCODE value, for a built program; `none` for a TAP without one. A manifest that records the TAP's IDCODE wins; set here against it, it's refused |
| `margin` | integer ≥ 1 | `8` | Sentinel bits fed past each register's length in a built program |
| `exhaustive_opcodes` | boolean | `true` | Test every unimplemented opcode in a built program |

## Notes on legacy and inert keys

```{note}
Some keys that appear in `config.ofs.example` are currently **not read** by the
parser and have no effect: `[simulation] tp_file`, `[report] verbose`, and the
whole `[debug]` section. The CLI also does **not** implement `--debug`/`--trace`;
logging is at INFO level. These are called out here only so you are not surprised
to see them in sample configs — extra observation points come from a separate
`tp_nodes.json` file (see [Architecture overview](../architecture/overview.md)),
not `tp_file`.
```
