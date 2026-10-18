# Cell libraries

faultflow needs to know the logic function and pin roles of every standard cell in a
netlist. It gets this from a **JSON cell map** — not from Liberty. The Liberty `.lib`
is used by Yosys only (for `dfflibmap` and `abc`); the C++ core never parses Liberty.

Two cell maps ship with the tool:

- `cells/sky130/sky130_fd_sc_hd.json` — SkyWater Sky130 HD (the default).
- `cells/osu/osu035.json` — OSU035.

Select one with `[design] cell_lib` (or `use_lib_cells sky130|osu035` in the shell).

Any other library's map is generated from its Liberty: `ff.py cell-map`, or
`[design] pdk` and `std_cell_library` for a PDK in the open_pdks layout. See
*Generated cell maps* below.

## Supported PDKs

Each library below is tested end to end: every gate and flop its map models is
simulated against the library's own Liberty, and a design is synthesized onto it, scan
inserted, ATPG'd, compressed and compacted, and the exported patterns replayed on the
library's own Verilog cell models; the IEEE 1500 wrapper's INTEST and EXTEST too
(with its WSP where the library has a falling-edge flop with a reset), and on
GF180MCU mbist-insert with JTAG.

| PDK (`pdk`) | Libraries (`std_cell_library`) | Not modelled |
|---|---|---|
| `sky130A` / `sky130B` | `sky130_fd_sc_hd` (shipped map), `_hdll`, `_hs`, `_ms`, `_ls`, `_lp`, `_hvl` | latches, tristates; `fahcin` / `fahcon` |
| `gf180mcuA`..`D` | `gf180mcu_fd_sc_mcu7t5v0`, `gf180mcu_fd_sc_mcu9t5v0` | latches, tristates |
| `ihp-sg13g2` | `sg13g2_stdcell` | latches, tristates |
| — | OSU035 (shipped map, no scan flop: generic scan) | |

Install them with [ciel](https://github.com/fossi-foundation/ciel) (`--pdk-family
sky130`, `gf180mcu` or `ihp-sg13`). GF180MCU's other libraries (`osu_sc_*`,
`as_sc_*`, `ocd_sc_*`) map too but have no scan flop. A library short of a cell a
flow needs is refused by that flow, naming the cell's role: Sky130 hdll and hvl and
IHP have no falling-edge flop with a reset for the WSP's WIR.

A design may also use cells of several libraries of one PDK, e.g. Sky130 hd logic
with hvl level shifters and flops (`[design] extra_libraries`): each flop is scanned
with its own library's scan flop, on chains of its own library; this is tested end to
end, the compressed and compacted chip replayed on both libraries' models.

## The cell-map format

A cell map is a JSON object whose keys are **drive-strength glob patterns** (prefix
match on `*`) and whose values describe the cell. The schema is
[`schemas/cell_map.schema.json`](https://github.com/ranaumarnadeem/faultflow/blob/main/schemas/cell_map.schema.json).
The first matching pattern wins.

| Field | Applies to | Meaning |
|---|---|---|
| `node_type` | all | `GATE`, `FF`, `LATCH`, `CONST`, `TBUF`, or `ICG` |
| `gate_type` | GATE/CONST | The normalized logic type (e.g. `OAI21`, `MUX2`) the engine evaluates |
| `inputs` | all | Ordered input pin names → `in0`, `in1`, … (this locks the pin order) |
| `outputs` | all | Map of logical output → physical port (e.g. `{"S": "YS", "CO": "YC"}` for an adder) |
| `ff` | FF/LATCH | Sequential metadata: trigger edge, clock/data/output nets, set/reset, scan pins |
| `status`, `reason` | optional | Mark a cell explicitly unsupported/deferred, with a reason |

A combinational example (drive strengths collapse via the `*`):

```json
"INVX*": {
  "node_type": "GATE",
  "gate_type": "INV",
  "inputs": ["A"],
  "outputs": {"Y": "Y"}
}
```

A flip-flop carries its sequential semantics in `ff`. This Sky130 HD entry is an
async-set (preset), positive-edge D flip-flop (`dfstp` — **D**-**F**F, **s**et,
**t**riangle/edge, **p**ositive):

```json
"sky130_fd_sc_hd__dfstp_*": {
  "node_type": "FF",
  "inputs": ["CLK", "D", "SET_B"],
  "outputs": {"Q": "Q"},
  "ff": {
    "clock": "CLK",
    "data": "D",
    "output": "Q",
    "trigger": "POSEDGE",
    "preset": {"pin": "SET_B", "level": "LOW", "value": 1}
  }
}
```

```{important}
The `expression` field that appears in some entries is **documentation only**. The
authoritative semantics are the `gate_type` enum plus the ordered `inputs` mapping,
interpreted by the C++ gate-evaluation code. Never treat `expression` as the source of
truth.
```

## How cells become gates

During normalization, each instance's type is matched against the patterns; the
`gate_type` selects a branch-free bitwise evaluation in `src/core/sim/gate_eval.cpp`.
Compound Sky130 cells (e.g. `a2bb2oi`, AOI/OAI families, `mux4`) are **first-class
`GateType` values with their own native evaluations** — faultflow does not decompose
them into primitive sub-gates. Multi-output cells (adders, flip-flops with `QN`) are
split into one node per output by the compiler.

A representative sample of the gate-evaluation table — the full set is much larger
(the Sky130 HD compound cell families alone run to dozens of `GateType` values):

| `GateType` | Evaluation | Notes |
|---|---|---|
| `INV` | `~in0` | |
| `BUF` | `in0` | |
| `AND2` / `OR2` / `NAND2` / `NOR2` | `in0 & in1` / `in0 \| in1` / negated | 3- and 4-input forms exist too |
| `XOR2` / `XNOR2` | `in0 ^ in1` / `~(in0 ^ in1)` | not fault-collapsible — see [Collapsing rules](../collapsing_rules.md) |
| `AOI21` / `OAI21` | `~((in0&in1)\|in2)` / `~((in0\|in1)&in2)` | compound cells are native, not decomposed |
| `MUX2` | `~((in2&in0)\|(~in2&in1))` | inverting mux; `in2` is select, `in2=1` selects `in0` |
| `ADDF` (`S`/`CO`) | `in0^in1^in2` / majority(`in0,in1,in2`) | full adder; lowered to two single-output nodes |
| `ADDH` (`S`/`CO`) | `in0^in1` / `in0&in1` | half adder; same lowering |
| `AND2B` | `in0 & ~in1` | one bubbled input — e.g. `sky130_fd_sc_hd__and2b_*`, pins `[B, A_N]` |
| `OR2B` | `in0 \| ~in1` | e.g. `sky130_fd_sc_hd__or2b_*`, pins `[A, B_N]` |
| `OR4BB` | `in0 \| in1 \| ~in2 \| ~in3` | e.g. `sky130_fd_sc_hd__or4bb_*`, pins `[A, B, C_N, D_N]` |
| `AND4BB` | `~in0 & ~in1 & in2 & in3` | e.g. `sky130_fd_sc_hd__and4bb_*`, pins `[A_N, B_N, C, D]` — first two bubbled |
| `A2BB2O` | `(~in0&~in1) \| (in2&in3)` | e.g. `sky130_fd_sc_hd__a2bb2o_*`, pins `[A1_N, A2_N, B1, B2]` |
| `O33AI` | `~((in0\|in1\|in2) & (in3\|in4\|in5))` | GF180MCU's `oai33` |
| `O222AI` | `~((in0\|in1) & (in2\|in3) & (in4\|in5))` | GF180MCU's `oai222` |
| `A222O` | `(in0&in1) \| (in2&in3) \| (in4&in5)` | Sky130 hs/ms/ls's `a222o` |

The `JSON expression` field on each cell-map entry documents the same formula for
humans — it is informational only, never authoritative (see the warning above).

## Unsupported and physical cells

Physical-only cells (fillers, taps, decaps, diodes, antenna/probe, power) are listed as
**explicit unsupported entries** rather than being absent. What happens when one is
encountered depends on `[simulation] unsupported_cells`:

- `fail` (default) — abort immediately; coverage is never reported on a netlist with
  unknown or unsupported cells.
- `blackbox` — model the cell as a test boundary; its outputs get no fault sites and
  are excluded from the denominator, and the exclusion is reported.

Tristate (`TBUF`) and latch cells are currently a hard error by policy; see the
[Roadmap](../roadmap.md).

## Generated cell maps

`faultflow/pdk/cell_map_gen.py` writes a library's map from its Liberty; the C++ core
still reads only the map.

- A combinational cell's `function` is matched to one of the engine's gate types by
  exhaustive truth table over every order of its input pins, the core's own gate
  evaluation the oracle. The match fixes `inputs`, and the compiler wires a gate's
  inputs to `in0`, `in1`, ... in that order (whatever its pins are named). Full and
  half adders are one entry with both outputs.
- A flop comes from its `ff` group (`clocked_on`, `next_state`, `clear`, `preset` and
  `clear_preset_var1`, the value with both active) and, for a scan flop, its
  `test_cell` (the `test_scan_in` / `test_scan_enable` pins). `next_state` must be
  the engine's model -- scan mux, then an enable that holds the state, then the data
  pin -- checked by truth table. A second, inverted output (`Q_N`) is
  `outputs.QN`: the engine drives it with an inverter reading `Q`.
- An integrated clock gate (`clock_gating_integrated_cell`: `latch_posedge` or
  `latch_negedge`, `_precontrol` with a test enable) is an `ICG` entry, its pins
  from their `clock_gate_{clock,enable,test,out}_pin` attributes: Sky130's
  `dlclkp`/`sdlclkp`, GF180MCU's `icgtp`/`icgtn`, IHP's `lgcp`/`slgcp`.
- Every other cell is listed, with why, never guessed: latches (`unsupported`),
  tristates (`TBUF`), tie and physical cells, and functions no gate type has.
- Drive strengths share a `<name>_*` pattern, whose `variants` name the cells, unless
  the pattern would match another cell.

Every generated map is checked end to end in the test suite: each gate and flop it
models, in every library installed, simulated through the core against its own
Liberty function or `ff` group.

FaultFlow's own cells (scan flops, the wrapper's boundary cells and WSP, the gates scan
stitching adds) come from the library by role (`faultflow/pdk/roles.py`): a role is a
logical function -- a non-inverting `mux2`, an `and2b`, a flop with an active-low
reset, a scan flop -- and the library's cell for it is found and checked with the same
oracle. A flop role the library has no cell of exactly its shape for takes a flop with
more controls, holding the rest inactive: IHP SG13G2 has reset flops only, so its
plain scan flop is `sg13g2_sdfrbpq` with `RESET_B` tied high. A gate role likewise
takes a wider gate with inputs tied off: GF180MCU and Sky130 hvl have no `and2b`
(`!a_n & b`), and their `mux2` with `A1` tied low is one. Likewise a flop
synthesis tied off a reset of (on a library without a plain flop) scans as a plain
one. A flow that needs a role the library lacks says which.

### Clock gates

A clock gate is simulated as its cell is built, with no X: a latch of `EN | TE`,
transparent while the clock is off (low for a `latch_posedge` gate, high for a
`latch_negedge` one), and the gated clock out of it -- `CLK & L`, or `CLK | ~L`.
The latch is always known: every clock-off phase loads it, and a scan test starts
with the clocks off. The flops behind a gate see its clock's edges, so a gate closed
in capture holds them, and an enable that changes while the clock is high (the flops
it clocks just updated) never glitches the clock.

Scan insertion scans a gated flop on its *root* clock -- the clock the gate, or a
chain of gates, reads -- and keeps its pin on the gate's clock. Every gate must be
open while the chains shift: a gate whose test enable is tied off (or unconnected)
gets the scan enable on it, and a gate without one becomes its library's gate with
one (role `icg`, or `icg_negedge`), its test enable on the scan enable; a flop
behind a gate its library has no such gate for stays out of scan
(`clock_gate_without_test`). A test enable the design drives is left alone, and
`scan-check` refuses a gate it can't show held open in shift. In the ATPG view a
gated flop captures `enable ? D : Q`, the gate evaluating to its enable (`GateType`
`ICG_EN`, `ICG_EN_CHAIN` behind another gate), so the enable logic is graded; the
scan enable is 0 in every capture.

A library's Liberty at a corner is its main file and the companion files of that corner
(`<corner>_<suffix>`) that characterize cells the main one lacks: Sky130 hvl's eight
level shifters are only in its dual-supply `tt_025C_3v30_lv1v80`, and map as `BUF`,
or `AND2` of `A` and `SLEEP_B` for the isolating ones.

A design on several libraries of one PDK (`[design] extra_libraries`) has one cell
map: the primary library's, then each extra library's entries, tagged
`"library": "<name>"` (`faultflow/pdk/resolve.py: combine_libraries`). Roles are the
primary library's alone. Scan stitching records an extra library's flop in its
generic scan cell's `FAULTFLOW_LIBRARY` parameter, and the scan techmap then has a
module per library and cell type, each mapping only its own library's cells
(`_TECHMAP_FAIL_`); with one library the techmap is as before.

The library's Verilog models are joined into one file for iverilog, its primitives
first. Two open_pdks defects are mended there: `sky130_fd_sc_lp.v` wraps 111 base
cells it never defines (each is given its `_1` variant's model, renamed), and some
versions of `sky130_fd_sc_hd.v` end a guard with a bare word after `` `endif ``.

## Adding cells

To support a new cell, add a pattern entry to the relevant JSON map with the correct
`node_type`, `gate_type`, ordered `inputs`, and `outputs`. If the cell's logic function
is not already a `GateType` the engine knows, that enum and its evaluation must be added
in the C++ core as well. The Sky130 map is filled in incrementally — cells used by the
target benchmarks first, with the remainder marked as explicit deferred entries.
