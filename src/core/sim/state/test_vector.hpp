#pragma once

#include <cstdint>
#include <map>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

struct TestCycle {
  std::map<int, bool> inputs;  // YosysNetID -> value
  bool sample_outputs = true;
  int settle_cycles = 0;
  bool fault_active = true;
};

struct TestVector {
  std::map<int, bool> inputs;  // YosysNetID -> value
  std::vector<TestCycle> cycles;
  std::map<uint32_t, bool> initial_ff_state;  // compiled FF index -> value
  // IEEE 1500 wrapper mode for the sequential scan-protocol sim. When != FUNCTIONAL
  // and the graph has wrapper cells, the sequential evaluators consult a ModeConfig
  // so a native shiftable WBR cell's mode-mux drives the core/interconnect from the
  // loaded FF state q (INTEST/EXTEST) instead of the transparent CFI passthrough.
  TestMode test_mode = TestMode::FUNCTIONAL;

  bool is_sequential() const { return !cycles.empty(); }
};

}  // namespace faultflow
