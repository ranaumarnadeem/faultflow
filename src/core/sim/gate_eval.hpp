#pragma once

#include <cstdint>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

uint64_t eval_gate(GateType type, const std::vector<uint64_t>& inputs);

bool eval_gate_scalar(GateType type, const std::vector<bool>& inputs);

inline bool is_icg(GateType type) {
  return type == GateType::ICG_POS || type == GateType::ICG_NEG;
}

// An integrated clock gate, one step: its latch `latch` takes `enable` (EN | TE)
// while the clock is off -- low for ICG_POS, high for ICG_NEG -- and holds while it
// is on, so an enable that changes while the clock is on (the flops it clocks
// just updated) never glitches the gated clock. Returns the gated clock:
// clk & L, or clk | ~L. Per lane: the 64 bits are independent universes.
inline uint64_t eval_icg(GateType type, uint64_t clk, uint64_t enable,
                         uint64_t& latch) {
  if (type == GateType::ICG_POS) {
    latch = (latch & clk) | (enable & ~clk);
    return clk & latch;
  }
  latch = (latch & ~clk) | (enable & clk);
  return clk | ~latch;
}

}  // namespace faultflow
