#pragma once

#include <cstdint>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

struct SimState {
  std::vector<std::vector<uint64_t>> frames;
  int current_frame = 0;
  std::vector<uint64_t> ff_states;
  std::vector<uint64_t> initial_ff_state;
  std::vector<uint64_t> prev_values;
  // IEEE 1500 test mode for this run. FUNCTIONAL by default; the engine applies
  // the per-mode wrapper control/observe reconfiguration via a ModeConfig.
  TestMode test_mode = TestMode::FUNCTIONAL;

  void init(int net_count, int frame_count = 1, int ff_count = 0) {
    frames.assign(frame_count, std::vector<uint64_t>(net_count, 0ULL));
    current_frame = 0;
    ff_states.assign(ff_count, 0ULL);
    initial_ff_state.assign(ff_count, 0ULL);
    prev_values.assign(net_count, 0ULL);
  }

  void reset_ff_states() { ff_states = initial_ff_state; }

  std::vector<uint64_t>& current_values() { return frames[current_frame]; }
  const std::vector<uint64_t>& current_values() const {
    return frames[current_frame];
  }
};

}  // namespace faultflow
