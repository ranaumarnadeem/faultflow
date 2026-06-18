#pragma once

#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow {

struct EnumeratorOptions {
  bool include_clock_faults = false;
  bool include_reset_faults = false;
};

std::vector<CompactFault> enumerate_faults(const NormalizedGraph& ng,
                                           const CompiledSimGraph& cg,
                                           EnumeratorOptions opt = {});

std::vector<CompactFault> enumerate_transition_faults(const NormalizedGraph& ng,
                                                       const CompiledSimGraph& cg,
                                                       EnumeratorOptions opt = {});

}  // namespace faultflow
