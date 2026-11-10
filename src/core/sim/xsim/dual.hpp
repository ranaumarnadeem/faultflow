#pragma once

#include <array>
#include <cstdint>

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
