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

  void broadcast_cycle_inputs(SimState& state, const CompiledSimGraph& cg,
                              const TestCycle& cycle) const;

  void evaluate_combinational(SimState& state, const CompiledSimGraph& cg,
                              const FaultBatch& batch) const;

  void inject_faults(std::vector<uint64_t>& nv, const FaultBatch& batch,
                     uint32_t net_index) const;

  uint64_t check_observation(const SimState& state, const CompiledSimGraph& cg,
                             const FaultBatch& batch) const;

  bool simulate_single_fault(const CompiledSimGraph& cg, const TestVector& vec,
                             const CompactFault& fault) const;

  // IEEE 1500 mode-aware single-fault simulation. Broadcasts PIs + ModeConfig
  // control points to all lanes, evaluates wrapper cells per mode, and checks
  // detection over the mode's observable set. Combinational only. Cross-checked
  // against GoldenRefSim's mode-aware oracle.
  bool simulate_single_fault(const CompiledSimGraph& cg, const TestVector& vec,
                             const CompactFault& fault,
                             const ModeConfig& mode) const;

  void evaluate_combinational_mode(SimState& state, const CompiledSimGraph& cg,
                                   const FaultBatch& batch,
                                   const ModeConfig& mode) const;

  uint64_t simulate_batch(const CompiledSimGraph& cg, const TestVector& vec,
                          const FaultBatch& batch) const;

  // IEEE 1500 mode-aware batch simulation. Broadcasts PIs + ModeConfig stimulus
  // nets, evaluates via evaluate_combinational_mode, and checks detection over
  // mc.observable_nets. Handles single-cycle combinational and multi-cycle
  // sequential vectors; WBR cells are evaluated per mode on every cycle.
  uint64_t simulate_batch(const CompiledSimGraph& cg, const TestVector& vec,
                          const FaultBatch& batch, const ModeConfig& mc) const;

  std::vector<std::vector<uint64_t>> simulate_batch_samples(
      const CompiledSimGraph& cg, const TestVector& vec,
      const FaultBatch& batch) const;

  void seed_ff_outputs(SimState& state, const CompiledSimGraph& cg,
                       const FaultBatch& batch) const;

  void update_ff_states(SimState& state, const CompiledSimGraph& cg) const;

  // Two-frame transition detection. Mirrors simulate_transition_fault (golden oracle).
  bool simulate_transition_single_fault(const CompiledSimGraph& cg, const TestVector& v1,
                                         const TestVector& v2,
                                         const CompactFault& fault) const;

  // Batched two-frame transition fault simulation over a launch/capture pair.
  // Returns the detected-lane mask (bit per lane, like simulate_batch): a lane
  // is detected iff the capture-frame stuck-at propagates to an observable AND
  // the good machine made the required transition at that lane's fault net.
  uint64_t simulate_transition_batch(const CompiledSimGraph& cg, const TestVector& v1,
                                     const TestVector& v2,
                                     const FaultBatch& batch) const;
};

}  // namespace faultflow
