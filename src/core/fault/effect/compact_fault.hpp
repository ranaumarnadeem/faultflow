#pragma once

#include <cstdint>
#include <limits>

#include "common/types.hpp"

namespace faultflow {

struct CompactFault {
  uint32_t net_index = 0;
  uint64_t sa_mask = 0;
  FaultType type = FaultType::SA0;
  uint8_t bit = 1;
  FaultStatus status = FaultStatus::PENDING;
  FaultExclusion exclusion = FaultExclusion::NONE;
  uint32_t collapsed_into = std::numeric_limits<uint32_t>::max();
  uint32_t detected_by_vector = 0;
  FaultModel model = FaultModel::STUCK_AT;
};

}  // namespace faultflow
