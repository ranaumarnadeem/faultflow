#pragma once

#include <cstdint>
#include <vector>

#include "fault/effect/fault_batch.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/state/test_vector.hpp"
#include "sim/xsim/dual.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

namespace faultflow {

// Per lane of a batch: `definite` where some sampled cycle shows a 0 against a 1
// at an observable net, `possible` where none does but the good machine is known
// and the faulty one X there (classify_detection, 63 faults at once).
struct XBatchDetection {
  uint64_t definite = 0ULL;
  uint64_t possible = 0ULL;
};

// The bit-parallel X engine: BitParallelSim's cycle loop on dual-rail words
// (lane 0 the good machine, lanes 1..63 a batch's faults), with GoldenRefSimX's
// semantics -- the X reference is its oracle. A separate engine: BitParallelSim
// is unchanged, and with no X source this one's h rail equals its words.
class BitParallelSimX {
 public:
  // Every net's words at each sampled cycle.
  std::vector<std::vector<Dual>> simulate_batch_samples(const CompiledSimGraph& cg,
                                                        const TestVector& vec,
                                                        const FaultBatch& batch,
                                                        const XRunOptions& options) const;

  XBatchDetection detect_batch(const CompiledSimGraph& cg, const TestVector& vec,
                               const FaultBatch& batch,
                               const XRunOptions& options) const;
};

}  // namespace faultflow
