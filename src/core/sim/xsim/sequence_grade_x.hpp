#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/engine/sequence_grade.hpp"
#include "sim/state/test_vector.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"
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

// The request's cycles as one vector, its unknowns in `options` (per cycle, the
// unknown inputs; the power-up state). Throws on a malformed request.
TestVector sequence_vector_x(const ParsedGraph& parsed, const CompiledSimGraph& cg,
                             const XSequenceGradeRequest& request, XRunOptions& options);

// A fault spec as the simulators take it. Throws on a bad net or fault type.
CompactFault sequence_fault(const SequenceFaultSpec& spec, const CompiledSimGraph& cg);

// observe_outputs' Yosys net ids and compiled net indexes. Throws on a name
// that is no compiled net.
void sequence_observed(const ParsedGraph& parsed, const CompiledSimGraph& cg,
                       const std::vector<std::string>& observe_outputs,
                       std::vector<int>& yids, std::vector<uint32_t>& compiled);

// Bit-parallel on BitParallelSimX, 63 faults per batch over sim_threads; each
// batch's good lane is checked against GoldenRefSimX and a disagreement throws.
// With reference, every fault is graded by GoldenRefSimX instead.
XSequenceGradeResult grade_sequence_faults_x(
    const std::string& json_path, const std::string& cell_map_path,
    const XSequenceGradeRequest& request, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads, bool reference);

}  // namespace faultflow
