#pragma once

#include <map>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow {

class GoldenRefSim {
 public:
  std::map<int, bool> simulate_fault_free(const CompiledSimGraph& cg,
                                          const TestVector& vec) const;

  std::map<int, bool> simulate_with_fault(const CompiledSimGraph& cg,
                                          const TestVector& vec,
                                          const CompactFault& fault) const;

  bool is_detected(const CompiledSimGraph& cg,
                   const std::map<int, bool>& fault_free,
                   const std::map<int, bool>& faulty) const;

  std::vector<std::map<int, bool>> simulate_sequence_fault_free(
      const CompiledSimGraph& cg, const TestVector& vec) const;

  std::vector<std::map<int, bool>> simulate_sequence_with_fault(
      const CompiledSimGraph& cg, const TestVector& vec,
      const CompactFault& fault) const;

  bool is_sequence_detected(
      const CompiledSimGraph& cg,
      const std::vector<std::map<int, bool>>& fault_free,
      const std::vector<std::map<int, bool>>& faulty) const;
};

}  // namespace faultflow
