#pragma once

#include <cstdint>

#include "fault/effect/compact_fault.hpp"

namespace faultflow {

inline constexpr int kBatchSize = 63;

struct FaultBatch {
  CompactFault faults[kBatchSize];
  int size = 0;
  uint64_t mask = 0;
};

}  // namespace faultflow
