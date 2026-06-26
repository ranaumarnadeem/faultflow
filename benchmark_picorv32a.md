# PicoRV32a Scan INTEST — Benchmark Results

> ⚠️ **Numbers below predate the Q-stem grading fix and must be regenerated.**
> These results were produced while scan-FF Q-stem stuck-at faults were graded by
> the capture-value-only bypass (commit `dc559ca`), which credited/rejected FF
> faults from each FF's own captured D instead of the fault's actual propagation —
> mis-grading roughly one fault per FF (~1,613 sites). The grading has since been
> corrected (reduced-view functional grade ∪ chain-integrity ∪ protocol-sim
> fallback). Re-run `scripts/run_picorv32a_intest_extest.py` and replace the
> detected/redundant/unresolved counts before citing these figures.


**Design:** PicoRV32a (RISC-V RV32IMC CPU core, open-source RTL by Claire Wolf)  
**Flow:** Yosys synthesis → Sky130 HD PDK → IEEE-1500 WBR wrapping → scan insertion → native SAT ATPG (INTEST)  
**Fault model:** Stuck-at (SA0/SA1), no fault collapsing  
**Host:** WSL2 (Ubuntu), Windows 11 Pro 10.0.26200, AMD/Intel x86-64  
**DB location:** `/mnt/c` (Windows NTFS via WSL 9P — significant I/O overhead; native Linux FS would be faster)  
**Date:** 2026-06-26

---

## Design Statistics

| Metric | Value |
|---|---|
| Design | PicoRV32a (RISC-V RV32IMC) |
| PDK | Sky130 HD (`sky130_fd_sc_hd`) |
| Total cells (post-synthesis, pre-wrap) | 12,602 |
| Combinational gates | 10,989 |
| Flip-flops | 1,613 |
| FF breakdown | 610 × dfxtp_1 (D) + 1,003 × edfxtp_1 (enable-D) |
| I/O ports | 30 |

---

## Scan Infrastructure

| Metric | Value |
|---|---|
| IEEE-1500 WBR model | `scan` (shiftable, full INTEST) |
| WBR cells added | 367 (101 wbc_in + 266 wbc_out) |
| Scan chains | 4 |
| Total scan cells | 1,613 |
| Average chain length | ~403 FFs |
| Scan enable | `scan_en` |
| Scan inputs | `scan_in_0 … scan_in_3` |
| Scan outputs | `scan_out_0 … scan_out_3` |

---

## Fault Statistics

| Category | Count |
|---|---|
| Total raw fault sites enumerated | 90,238 |
| **In-denominator (active fault sites)** | **75,654** |
| Excluded — scan internal (shift-path) | 6,198 |
| Excluded — clock nets | 3,962 |
| Excluded — scan chain infrastructure | 2,944 |
| Excluded — WBR decoupled (blackboxed) | 1,138 |
| Excluded — reset nets | 342 |
| Total excluded | 14,584 |
| Fault collapsing | **DISABLED** (see note below) |

---

## ATPG Results

| Metric | Value |
|---|---|
| Detected | **73,712** |
| Proven redundant (SAT → UNSAT) | 1,626 |
| Unresolved at kill time | 316 |
| Total resolved | 75,338 / 75,654 = **99.58%** |
| **Formal fault coverage** | **73,712 / 75,654 = 97.44%** |
| Achievable coverage (excl. proven redundant) | 73,712 / 74,028 = **99.57%** |
| Test vectors | **1,296** (all SAT-generated; 0 random) |

---

## ATPG Configuration

| Parameter | Value |
|---|---|
| Engine | Native SAT ATPG (CaDiCaL) |
| Parallel workers | 4 |
| Timeout schedule | 2 s → 10 s → 60 s (escalating per fault) |
| Fault ordering | Cone-of-influence size (small cones first) |
| Compaction | Dynamic SAT-verified cube packing |
| Wave interleaving | 2 easy slots + 2 hard slots per wave |
| Max rounds | 20 |
| Coverage target | 90% |

---

## Timing

| Phase | Wall clock |
|---|---|
| Yosys synthesis (Sky130 HD) | ~3 min (pre-computed) |
| IEEE-1500 WBR wrap + scan insert | ~1 min |
| Scan chain structural validation | **459.4 s (~7.7 min)** |
| ATPG (killed before full convergence) | **~3 hours** |
| ATPG started | 2026-06-26T10:33:50 UTC |
| ATPG killed | 2026-06-26T~13:33 UTC |

Note: DB lives on `/mnt/c` (Windows NTFS via WSL 9P), which adds measurable I/O latency
per transaction. On a native Linux FS the same run would complete faster.

---

## Why Fault Collapsing Was Not Used

Fault collapsing reduces the number of fault sites in the denominator by removing
equivalent and dominated faults, lowering the vector count needed for high coverage.
faultflow has it implemented for primitive cells but it was **intentionally disabled** here
for two reasons:

### 1. Sky130 AOI/OAI cells cannot be safely collapsed yet

faultflow's collapsing rules are verified only for primitive gates: INV, BUF, AND2/NAND2,
OR2/NOR2. The Sky130 HD synthesis produces many compound cells — o21ai, a21o, a21oi,
o22ai, a22oi, a22o, a32oi, o211ai, … — where equivalence and dominance across fanout-split
branches have not been formally verified. The CLAUDE.md policy is explicit: *"Never collapse
compound cells (AOI/OAI) without verified truth-table analysis."* Collapsing with unverified
rules can silently remove fault sites that are actually detectable, producing coverage numbers
that are higher than reality.

### 2. Reduced scan view changes dominance relationships

The scan ATPG runs on a *fused view* — a reduced netlist where scan FFs are abstracted into
pseudo-PI/PO ports. Equivalence and dominance relationships that hold in the full netlist do
not necessarily hold in this reduced representation. Branch-fault collapsing would need to be
re-derived for the reduced topology, which has not been done.

### What collapsing would change

With collapsing, roughly 30–40% of fault sites would be removed from the denominator (typical
for combinational-equivalent coverage). The detected count would drop proportionally, so
**formal coverage % would be roughly the same or slightly higher** — but fewer SAT calls would
be needed per round, cutting ATPG runtime substantially. The 1,296-vector count would also
shrink.

### What the numbers mean without collapsing

Every fault site in the 75,654-site denominator was independently targeted. The 1,296 vectors
are a test set that directly detects 73,712 individually-verified fault sites and leaves only
316 unresolved (316 / 75,654 = **0.42%** of the denominator still pending when the run was
killed). The 1,626 proven-redundant sites were each verified by CaDiCaL as having no
detectable distinguishing vector under the current netlist, PDK, and observation model.
