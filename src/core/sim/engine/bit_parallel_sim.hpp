#pragma once

#include <cstdint>
#include <vector>

#include "fault/effect/fault_batch.hpp"
#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/state/sim_state.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow {

class BitParallelSim {
 public:
  void broadcast_inputs(SimState& state, const CompiledSimGraph& cg,
                        const TestVector& vec) const;

  void evaluate_combinational(SimState& state, const CompiledSimGraph& cg,
                              const FaultBatch& batch) const;

  void inject_faults(std::vector<uint64_t>& nv, const FaultBatch& batch,
                     uint32_t net_index) const;

  uint64_t check_observation(const SimState& state, const CompiledSimGraph& cg,
                             const FaultBatch& batch) const;

  bool simulate_single_fault(const CompiledSimGraph& cg, const TestVector& vec,
                             const CompactFault& fault) const;

  uint64_t simulate_batch(const CompiledSimGraph& cg, const TestVector& vec,
                          const FaultBatch& batch) const;
};

}  // namespace faultflow
