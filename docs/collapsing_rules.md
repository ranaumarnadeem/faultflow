# Fault Collapsing Rules

faultflow collapses stuck-at faults using **equivalence only** (never dominance): two faults are
collapsed only when they have **identical** detecting-vector sets, so coverage is provably
unchanged. A fault may be collapsed into another only when every *input* member of its equivalence
class is **fanout-free** (drives exactly one gate) — otherwise the input fault has additional
downstream effects and the equivalence no longer holds in-circuit.

## Primitive gates (implemented since Phase 1)

Derived from the controlling-value argument (Abramovici Ch. 5):

| Gate | Equivalence (input ≡ output) |
|---|---|
| INV | in_SA0 ≡ out_SA1 ; in_SA1 ≡ out_SA0 |
| BUF | in_SA0 ≡ out_SA0 ; in_SA1 ≡ out_SA1 |
| AND2/3/4 | each in_SA0 ≡ out_SA0 |
| OR2/3/4 | each in_SA1 ≡ out_SA1 |
| NAND2/3/4 | each in_SA0 ≡ out_SA1 |
| NOR2/3/4 | each in_SA1 ≡ out_SA0 |

The non-controlling polarity is a *dominance* relation and is deliberately **not** collapsed.

## XOR / XNOR — NOT collapsible

A 2-input XOR (or XNOR) has **no** equivalent faults: all six faults have distinct detecting-vector
sets (each input is symmetric and there is no controlling value). Collapsing them would remove
detectable faults and inflate coverage. The collapser leaves XOR/XNOR faults untouched, and
`test_xor_xnor_not_collapsed` locks this in.

## Compound AOI/OAI cells (this change)

Equivalence classes derived exhaustively by `scripts/derive_collapsing_rules.py` (which enumerates
every fault's detecting-vector set over the full 2^N truth table and groups identical sets).
`inN` is the cell-map input order. Each class drops `len(class) - 1` faults per cell instance,
subject to the fanout-free guard on input members.

| Gate (sky130 cell) | Function | Equivalence classes |
|---|---|---|
| `A21OI` (a21oi, =AOI21) | ~((A1&A2)\|B1) | {in0_SA0, in1_SA0} ; {in2_SA1, out_SA0} |
| `O21AI` (o21ai, =OAI21) | ~((A1\|A2)&B1) | {in0_SA1, in1_SA1} ; {in2_SA0, out_SA1} |
| `A22OI` (a22oi, =AOI22) | ~((A1&A2)\|(B1&B2)) | {in0_SA0, in1_SA0} ; {in2_SA0, in3_SA0} |
| `O22AI` (o22ai, =OAI22) | ~((A1\|A2)&(B1\|B2)) | {in0_SA1, in1_SA1} ; {in2_SA1, in3_SA1} |
| `A21O` (a21o) | (A1&A2)\|B1 | {in0_SA0, in1_SA0} ; {in2_SA1, out_SA1} |
| `O21A` (o21a) | (A1\|A2)&B1 | {in0_SA1, in1_SA1} ; {in2_SA0, out_SA0} |
| `A22O` (a22o) | (A1&A2)\|(B1&B2) | {in0_SA0, in1_SA0} ; {in2_SA0, in3_SA0} |
| `O22A` (o22a) | (A1\|A2)&(B1\|B2) | {in0_SA1, in1_SA1} ; {in2_SA1, in3_SA1} |

Notes:
- The two AND/OR-term inputs (in0,in1 for the `2…` term; in2,in3 for `…2` terms) are symmetric,
  so their controlling-value faults are input↔input equivalent.
- The single (non-paired) input of `21`-family cells (in2) is input↔output equivalent with the
  output, with polarity set by whether the cell inverts (AOI/OAI vs the non-inverting A21O/O21A).
- `A22*`/`O22*` have no single input, so they have only input↔input classes (no input↔output).

Representative selection: when a class contains the output fault, the **output** fault is kept and
the input faults collapse into it (matching the primitive convention); otherwise the first input
fault is the representative.

Higher AOI/OAI families (A31*, A221*, O211*, …) are a follow-up: extend `GATES` in the derivation
script, regenerate, add the class to the C++ table, and add the exhaustive test.
