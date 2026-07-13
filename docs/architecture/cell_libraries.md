# Cell libraries

faultflow needs to know the logic function and pin roles of every standard cell in a
netlist. It gets this from a **JSON cell map** — not from Liberty. The Liberty `.lib`
is used by Yosys only (for `dfflibmap` and `abc`); the C++ core never parses Liberty.

Two cell maps ship with the tool:

- `cells/sky130/sky130_fd_sc_hd.json` — SkyWater Sky130 HD (the default).
- `cells/osu/osu035.json` — OSU035.

Select one with `[design] cell_lib` (or `use_lib_cells sky130|osu035` in the shell).

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

## Adding cells

To support a new cell, add a pattern entry to the relevant JSON map with the correct
`node_type`, `gate_type`, ordered `inputs`, and `outputs`. If the cell's logic function
is not already a `GateType` the engine knows, that enum and its evaluation must be added
in the C++ core as well. The Sky130 map is filled in incrementally — cells used by the
target benchmarks first, with the remainder marked as explicit deferred entries.
