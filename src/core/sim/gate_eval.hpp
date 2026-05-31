#pragma once

#include <cstdint>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

uint64_t eval_gate(GateType type, const std::vector<uint64_t>& inputs);

bool eval_gate_scalar(GateType type, const std::vector<bool>& inputs);

}  // namespace faultflow
