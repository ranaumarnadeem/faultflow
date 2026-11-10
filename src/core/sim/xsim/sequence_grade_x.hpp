#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "sim/engine/sequence_grade.hpp"
#include "sim/xsim/logic.hpp"

namespace faultflow {

// grade_sequence_faults with unknown values. `base` is the two-valued request:
// its inputs, samples, outputs, faults and fault_active are used as they are;
// its initial_ff_value only when initial_unknown is false.
struct XSequenceGradeRequest {
  SequenceGradeRequest base;
  // Every flop and clock-gate latch unknown before cycle 0 (else as base says,
  // clock-gate latches at 0, as in the two-valued grader).
  bool initial_unknown = true;
  // Per cycle, per input_order entry: unknown that cycle. Empty: nothing is.
  std::vector<std::vector<bool>> unknown;
};

struct XSequenceGradeResult {
  // The good machine's observed values per sampled cycle (0, 1 or X).
  std::vector<std::vector<Logic>> golden;
  // Per fault, the first sampled cycle at which an observed output shows a 0
  // against a 1 (definite), or the good machine known and the faulty one X
  // (possible); -1 when none does. A fault is definitely detected when
  // first_definite >= 0, possibly detected when only first_possible is.
  std::vector<int32_t> first_definite;
  std::vector<int32_t> first_possible;
};

// Bit-parallel on BitParallelSimX, 63 faults per batch over sim_threads; each
// batch's good lane is checked against GoldenRefSimX and a disagreement throws.
// With reference, every fault is graded by GoldenRefSimX instead.
XSequenceGradeResult grade_sequence_faults_x(
    const std::string& json_path, const std::string& cell_map_path,
    const XSequenceGradeRequest& request, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads, bool reference);

}  // namespace faultflow
