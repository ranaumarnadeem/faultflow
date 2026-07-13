# Test-point insertion

This document describes the `add_tp` / `reject_tp` [Tcl shell](tcl_shell.md) commands.
They let faultflow users drive OpenTestability's one-shot TPI flow from within the interactive
shell, compare coverage before and after insertion, and revert if the result is unsatisfactory.
These commands are shell-only — there is no batch-CLI equivalent.

---

## Prerequisites

1. **OpenTestability installed.** Set the path in `config.ofs`:
   ```ini
   [testpoint]
   opentest = /path/to/venv/bin/opentest
   metric    = scoap
   threshold = 50
   max_points = 10
   ```
   If `opentest` is on `PATH` you can write `opentest = opentest`.

2. **Baseline sim campaign complete.** Run `sim` (or `run_atpg`) before calling `add_tp`.
   The baseline campaign is the reference against which the TP campaign is compared.

---

## Workflow

```tcl
read_netlist design.json -top my_top
use_lib_cells sky130
run_atpg

# Insert test points, re-sim, show comparison
add_tp

# Optionally revert
reject_tp
```

---

## `add_tp` — Insert and compare

```
add_tp [-m METRIC] [-t THRESHOLD] [-n MAX_POINTS]
```

| Flag | Default | Description |
|---|---|---|
| `-m METRIC` | `scoap` | Testability metric used by OT for candidate ranking (`scoap`, `cop`) |
| `-t THRESHOLD` | `50` | OT's hard-to-detect threshold (0–100) |
| `-n MAX_POINTS` | `10` | Maximum number of test points to insert |

**What happens:**

1. Calls `opentest --yosys analyze_and_add_tp` on the current netlist, writing a new
   `*_tp_yosys.json` under `output/<top>/tp/`.
2. Loads the TP netlist (skips re-synthesis — it's already a Yosys JSON), initialises a new
   campaign, and runs ATPG.
3. Computes coverage metrics and a "rescued" count: original faults (excluding
   `tp_obs_*` / `tp_ctrl_*` nets added by OT) that were undetected in the baseline but are
   now detected.
4. Prints a **Baseline | +TP | Δ** comparison table.
5. Pushes the new netlist onto the version stack and calls `checkpoint()`. The TP netlist
   becomes the active version.

**Comparison table columns:**

| Row | Meaning |
|---|---|
| Test coverage % | Structural test coverage |
| Fault coverage % | Stuck-at fault coverage |
| Detected / Denominator / Undetected | Raw fault counts |
| Original faults rescued | Faults on original nets, undetected in baseline, now detected |
| Test vectors | ATPG vector count |
| ATPG / Sim / Total time (s) | Timing breakdown |
| ATPG rounds | Number of ATPG rounds |
| Test points inserted (obs/ctrl) | OT's insertion report |

**The TP netlist is kept by default.** Use `reject_tp` to revert.

---

## `reject_tp` — Revert last iteration

```
reject_tp
```

Pops the most recent TP iteration from the version stack and restores the previous netlist
as the active source. The campaign database retains both campaigns' data; only the active
netlist pointer changes. Calls `checkpoint()`.

You cannot revert past the original baseline (iter 0). Attempting to do so raises an error.

---

## Version stack

Each `add_tp` call creates a new iteration, tracked in `output/<top>/tp/state.json`:

```json
{
  "versions": [
    {"iter": 0, "netlist_path": "output/my_top/my_top.json",    "campaign_id": 1, "label": "baseline"},
    {"iter": 1, "netlist_path": "output/my_top/tp/my_top_tp.json", "campaign_id": 2, "label": "tp_iter1"}
  ]
}
```

You can call `add_tp` multiple times to explore different thresholds or metrics, then
`reject_tp` to step back through the history.

---

## PDK and TPI cell support

faultflow automatically passes `--tech sky130` (or `--tech osu035`) to OT based on the
active cell library. The TPI cells each PDK inserts are fully supported:

**Sky130 HD** (default)
- Observe: `sky130_fd_sc_hd__buf_1` → `BUF`
- Control: `sky130_fd_sc_hd__mux2_1` → `MUX2_NI` (non-inverting: `Y = (~S&A0)|(S&A1)`)

**OSU035**
- Observe: `BUFX2` → `BUF`
- Control: `MX2X1/MX2X2/MX2X4` → `MUX2` (inverting: `Y = ~((S0&A)|(~S0&B))`)

The active PDK is determined from `[design] cell_lib` in `config.ofs`.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `opentest binary not found` | `opentest` not on PATH or wrong path | Set `[testpoint] opentest = /abs/path` |
| `opentest failed (exit N)` | OT subprocess error | Check OT logs; ensure input JSON has no `$_DFF_*` primitives (run `dfflibmap` upstream so FF cells are PDK-mapped) |
| `cannot pop: only baseline remains` | `reject_tp` called with nothing to revert | Run `add_tp` first |
| `unsupported cell: MX2X1` | Old faultflow without M0 patch | Update to current faultflow |
