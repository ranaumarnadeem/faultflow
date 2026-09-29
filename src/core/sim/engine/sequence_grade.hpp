#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace faultflow {

// Stuck-at fault grading over one multi-cycle input sequence, observing only
// the named outputs at the sampled cycles -- e.g. a JTAG program played through
// TCK and compared at TDO. Faults are explicit (compiled net index, 0 = SA0,
// 1 = SA1), not enumerated here: the caller's own exclusions decide what runs.
struct SequenceFaultSpec {
  uint32_t compiled_net_index = 0;
  uint8_t fault_type = 0;
};

struct SequenceGradeRequest {
  std::vector<std::string> input_order;       // primary-input names, one bit each
  std::vector<std::vector<bool>> cycles;      // per cycle, one value per input_order name
  std::vector<bool> sample;                   // per cycle: compare the observed outputs
  std::vector<std::string> observe_outputs;   // output names, one bit each
  std::vector<SequenceFaultSpec> faults;
  bool initial_ff_value = false;              // every flop's value before cycle 0
};

struct SequenceGradeResult {
  // Fault-free observed values, per sampled cycle, per observe_outputs entry
  // (the scalar reference simulator's).
  std::vector<std::vector<bool>> golden;
  // Per fault: index of the first sampled cycle at which an observed output
  // differs from `golden`; -1 when none does.
  std::vector<int32_t> first_sample;
};

// Bit-parallel grading, 63 faults per batch, batches spread over sim_threads
// (<= 0: hardware concurrency). Every batch's fault-free lane is checked
// against the scalar reference and a disagreement throws. With reference,
// every fault is graded by the scalar simulator instead (slow; for checking).
SequenceGradeResult grade_sequence_faults(
    const std::string& json_path, const std::string& cell_map_path,
    const SequenceGradeRequest& request, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads,
    bool reference);

}  // namespace faultflow
