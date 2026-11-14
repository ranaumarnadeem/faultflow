#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

// 64 lanes of 0/1/X as two rails: h = can be 1, l = can be 0. A lane is 0 at
// (h,l) = (0,1), 1 at (1,0), X at (1,1); (0,0) never occurs. With no X, h is the
// two-valued engine's word and l its complement.
struct Dual {
  uint64_t h = 0;
  uint64_t l = ~0ULL;
};

inline Dual dual_known(uint64_t word) { return {word, ~word}; }
inline Dual dual_x() { return {~0ULL, ~0ULL}; }

inline Dual dual_not(Dual a) { return {a.l, a.h}; }
inline Dual dual_and(Dual a, Dual b) { return {a.h & b.h, a.l | b.l}; }
inline Dual dual_or(Dual a, Dual b) { return {a.h | b.h, a.l & b.l}; }

// sel ? if1 : if0 per lane, exact for an unknown select: a lane whose data inputs
// agree on a known value keeps it.
inline Dual dual_mux(Dual sel, Dual if0, Dual if1) {
  return {(sel.l & if0.h) | (sel.h & if1.h), (sel.l & if0.l) | (sel.h & if1.l)};
}

// The lanes known in `a`.
inline uint64_t dual_known_lanes(Dual a) { return a.h ^ a.l; }

// A gate over 64 lanes of 0/1/X, exact for every cell: h is set where some 1-cube
// of the cell's function is consistent with the inputs (a literal a reads h_a, ~a
// reads l_a), l where some 0-cube is. The cubes are the prime implicants of the
// two-valued function, derived once from eval_gate_scalar, so this engine has no
// gate formulas of its own. Unused inputs read 0.
Dual eval_gate_dual(GateType type, const std::array<Dual, 6>& inputs);

// A cell's cubes laid out for the hot loop (dual_gate, eval_dual_gate). A cube
// is `size` rails: input i at 1 reads rail 2i (its h), at 0 rail 2i + 1 (its l).
struct DualRailCube {
  uint8_t size = 0;
  std::array<uint8_t, 6> rail{};
};
struct DualGate {
  const DualRailCube* ones = nullptr;
  const DualRailCube* ones_end = nullptr;
  const DualRailCube* zeros = nullptr;
  const DualRailCube* zeros_end = nullptr;
  uint8_t relevant = 0;  // the inputs some cube reads: those the function depends on
  uint8_t arity = 0;     // 1 + the highest of them; inputs past it are never read
  bool known_by_ones = true;  // with every lane known, the cheaper side to evaluate
};
// nullptr for a type with no combinational function.
const DualGate* dual_gate(GateType type);

inline uint64_t dual_terms(const DualRailCube* c, const DualRailCube* end,
                           const uint64_t* rails) {
  uint64_t out = 0ULL;
  for (; c != end; ++c) {
    uint64_t term = ~0ULL;
    for (uint8_t k = 0; k < c->size; ++k) {
      term &= rails[c->rail[k]];
    }
    out |= term;
  }
  return out;
}

// eval_gate_dual on a dual_gate; reads inputs[0 .. arity) only. Where every lane
// is known at every input the function reads, one side of the cubes decides
// both rails (a known lane is 1 exactly where a 1-cube holds, 0 where a 0-cube
// does), the common case of a run with few unknowns.
inline Dual eval_dual_gate(const DualGate& g, const std::array<Dual, 6>& inputs) {
  uint64_t rails[12];
  uint64_t known = ~0ULL;
  for (uint8_t i = 0; i < g.arity; ++i) {
    rails[2 * i] = inputs[i].h;
    rails[2 * i + 1] = inputs[i].l;
    if ((g.relevant >> i) & 1) {
      known &= inputs[i].h ^ inputs[i].l;
    }
  }
  if (known == ~0ULL) {
    if (g.known_by_ones) {
      const uint64_t h = dual_terms(g.ones, g.ones_end, rails);
      return {h, ~h};
    }
    const uint64_t l = dual_terms(g.zeros, g.zeros_end, rails);
    return {~l, l};
  }
  return {dual_terms(g.ones, g.ones_end, rails), dual_terms(g.zeros, g.zeros_end, rails)};
}

// Both sides of the cubes always (never the known-lane shortcut): for tests.
Dual eval_gate_dual_cubes(GateType type, const std::array<Dual, 6>& inputs);

// The cubes eval_gate_dual evaluates, for a dual-rail CNF of the same cell (the
// X-mode SAT ATPG): a product of the inputs in `care`, each at its bit in
// `value` (bit i = input i). `ones` cover the function, `zeros` its
// complement. Empty and invalid for a type with no combinational function.
struct DualCube {
  uint8_t care = 0;
  uint8_t value = 0;
};
struct DualCubes {
  bool valid = false;
  std::vector<DualCube> ones;
  std::vector<DualCube> zeros;
};
const DualCubes& dual_cubes(GateType type);

// eval_icg_x over 64 lanes.
inline Dual eval_icg_dual(GateType type, Dual clk, Dual enable, Dual& latch) {
  if (type == GateType::ICG_POS) {
    latch = dual_mux(clk, enable, latch);
    return dual_and(clk, latch);
  }
  latch = dual_mux(clk, latch, enable);
  return dual_or(clk, dual_not(latch));
}

}  // namespace faultflow
