#pragma once

#include <set>
#include <string>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow {

struct EnumeratorOptions {
  bool include_clock_faults = false;
  bool include_reset_faults = false;
  // Sites a flow excludes as clock or reset itself (canonical site keys), whatever
  // the netlist's own tags and the include_ flags say. Tagged here, they are known
  // to the collapser, which collapses nothing into an excluded fault.
  std::set<std::string> clock_sites;
  std::set<std::string> reset_sites;
};

std::vector<CompactFault> enumerate_faults(const NormalizedGraph& ng,
                                           const CompiledSimGraph& cg,
                                           EnumeratorOptions opt = {});

std::vector<CompactFault> enumerate_transition_faults(const NormalizedGraph& ng,
                                                       const CompiledSimGraph& cg,
                                                       EnumeratorOptions opt = {});

}  // namespace faultflow
