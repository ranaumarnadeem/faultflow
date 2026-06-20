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

  // IEEE 1500 mode-aware combinational oracle. Stimulus is broadcast into the
  // ModeConfig control points (which may be internal core/sys nets, not just
  // PIs); wrapper cells are evaluated per mode (buffer / skip-stimulus /
  // safe-0); detection is checked over the mode's observable set. Combinational
  // only (the wrapper infrastructure step has no sequential wrapper chain).
  std::map<int, bool> simulate_fault_free(const CompiledSimGraph& cg,
                                          const TestVector& vec,
                                          const ModeConfig& mode) const;

  std::map<int, bool> simulate_with_fault(const CompiledSimGraph& cg,
                                          const TestVector& vec,
                                          const CompactFault& fault,
                                          const ModeConfig& mode) const;

  bool is_detected(const CompiledSimGraph& cg,
                   const std::map<int, bool>& fault_free,
                   const std::map<int, bool>& faulty,
                   const ModeConfig& mode) const;

  std::vector<std::map<int, bool>> simulate_sequence_fault_free(
      const CompiledSimGraph& cg, const TestVector& vec) const;

  std::vector<std::map<int, bool>> simulate_sequence_with_fault(
      const CompiledSimGraph& cg, const TestVector& vec,
      const CompactFault& fault) const;

  bool is_sequence_detected(
      const CompiledSimGraph& cg,
      const std::vector<std::map<int, bool>>& fault_free,
      const std::vector<std::map<int, bool>>& faulty) const;

  // Two-frame transition oracle. Returns true iff:
  //   1. good machine made the required 0->1 (STR) / 1->0 (STF) transition at the fault net, AND
  //   2. the capture-frame stuck-at is observable at a PO/TP.
  bool simulate_transition_fault(const CompiledSimGraph& cg, const TestVector& v1,
                                  const TestVector& v2,
                                  const CompactFault& fault) const;
};

}  // namespace faultflow
