#pragma once

#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow {

std::vector<CompactFault> collapse_primitive_faults(
    const NormalizedGraph& ng, const CompiledSimGraph& cg,
    std::vector<CompactFault> faults);

}  // namespace faultflow
