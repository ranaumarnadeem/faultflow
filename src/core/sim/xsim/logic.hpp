#pragma once

#include <cstdint>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

// One net's value in the X engine. Every cell input reads Z as X; a cell drives
// 0, 1 or X, never Z (only a tristate bus resolver will drive Z).
enum class Logic : uint8_t { ZERO = 0, ONE = 1, X = 2, Z = 3 };

inline Logic logic_of(bool value) { return value ? Logic::ONE : Logic::ZERO; }

inline bool is_known(Logic v) { return v == Logic::ZERO || v == Logic::ONE; }

inline Logic logic_not(Logic a) {
  return is_known(a) ? logic_of(a == Logic::ZERO) : Logic::X;
}

inline Logic logic_and(Logic a, Logic b) {
  if (a == Logic::ZERO || b == Logic::ZERO) {
    return Logic::ZERO;
  }
  return (a == Logic::ONE && b == Logic::ONE) ? Logic::ONE : Logic::X;
}

inline Logic logic_or(Logic a, Logic b) {
  if (a == Logic::ONE || b == Logic::ONE) {
    return Logic::ONE;
  }
  return (a == Logic::ZERO && b == Logic::ZERO) ? Logic::ZERO : Logic::X;
}

// sel ? if1 : if0, exact for an unknown select: known when both data inputs are
// the same known value.
inline Logic logic_mux(Logic sel, Logic if0, Logic if1) {
  if (sel == Logic::ZERO) {
    return is_known(if0) ? if0 : Logic::X;
  }
  if (sel == Logic::ONE) {
    return is_known(if1) ? if1 : Logic::X;
  }
  return (is_known(if0) && if0 == if1) ? if0 : Logic::X;
}

// A gate's output over 0/1/X/Z inputs, by the cell's two-valued function
// (eval_gate_scalar): the value every completion of the unknown inputs gives, or
// X when completions disagree. Exact for every cell, not a per-gate formula, so it
// is the X engine's oracle. A missing input reads 0, as in eval_gate_scalar.
Logic eval_gate_x(GateType type, const std::vector<Logic>& inputs);

// eval_icg with unknowns: the latch takes `enable` while the clock is off (low
// for ICG_POS, high for ICG_NEG) and holds while it is on, so with the clock
// unknown it keeps a value only where the enable agrees with it.
Logic eval_icg_x(GateType type, Logic clk, Logic enable, Logic& latch);

}  // namespace faultflow
