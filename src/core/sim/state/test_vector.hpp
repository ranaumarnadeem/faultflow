#pragma once

#include <cstdint>
#include <map>
#include <vector>

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

  bool is_sequential() const { return !cycles.empty(); }
};

}  // namespace faultflow
