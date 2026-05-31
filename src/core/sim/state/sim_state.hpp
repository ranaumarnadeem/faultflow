#pragma once

#include <cstdint>
#include <vector>

namespace faultflow {

struct SimState {
  std::vector<std::vector<uint64_t>> frames;
  int current_frame = 0;

  void init(int net_count, int frame_count = 1) {
    frames.assign(frame_count, std::vector<uint64_t>(net_count, 0ULL));
    current_frame = 0;
  }

  std::vector<uint64_t>& current_values() { return frames[current_frame]; }
  const std::vector<uint64_t>& current_values() const {
    return frames[current_frame];
  }
};

}  // namespace faultflow
