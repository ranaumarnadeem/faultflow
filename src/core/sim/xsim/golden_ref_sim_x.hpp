#pragma once

#include <map>
#include <set>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/state/test_vector.hpp"
#include "sim/xsim/logic.hpp"

namespace faultflow {

struct XRunOptions {
  // Every flop and clock-gate latch before cycle 0. TestVector::initial_ff_state
  // still sets a flop to a known value.
  Logic initial_state = Logic::X;
  // Per cycle, the primary inputs (Yosys net ids) that are unknown that cycle;
  // shorter than the sequence (or empty): none after its end.
  std::vector<std::set<int>> unknown_inputs;
};

// One sampled cycle: a value per Yosys net id, as GoldenRefSim snapshots.
using XSnapshot = std::map<int, Logic>;

// DEFINITE: some sample shows a 0 against a 1 at an observed net. POSSIBLE: none
// does, but at some observed net the good machine is known and the faulty one X.
// Nothing is credited where the good machine itself is X.
enum class XDetection { NONE, POSSIBLE, DEFINITE };

// The scalar oracle of the X engine: GoldenRefSim's sequence, cycle by cycle, on
// 0/1/X/Z values. Gates by eval_gate_x; a flop's next state as exact muxes over
// its scan select, data enable, clock edge, clear and preset, so an unknown
// control leaves it known only where both choices agree. Net values before cycle 0
// are 0, as in GoldenRefSim, so cycle 0 has no clock edge.
class GoldenRefSimX {
 public:
  // `fault`: null, or a fault (ignored when excluded, as GoldenRefSim does).
  // `sample_yids`: null for every net, else only these.
  std::vector<XSnapshot> simulate_sequence(const CompiledSimGraph& cg,
                                           const TestVector& vec,
                                           const CompactFault* fault,
                                           const XRunOptions& options,
                                           const std::vector<int>* sample_yids =
                                               nullptr) const;

  // The two-frame transition oracle: the launch frame `v1` fault-free, the
  // capture frame `v2` with the fault as a stuck-at (SA0 slow to rise, SA1 slow
  // to fall). Nothing is credited unless the good machine at the fault net is
  // known at the pre-transition value in the launch frame and at the post one in
  // the capture frame (an unknown launch may never make the edge); given that,
  // the detection as classify_detection grades it.
  XDetection simulate_transition_fault(const CompiledSimGraph& cg, const TestVector& v1,
                                       const TestVector& v2, const CompactFault& fault,
                                       const XRunOptions& options) const;
};

XDetection classify_detection(const CompiledSimGraph& cg,
                              const std::vector<XSnapshot>& good,
                              const std::vector<XSnapshot>& faulty);

// The launch/capture pair as one two-cycle vector: both cycles sampled, the
// fault active in the capture cycle only (BitParallelSim's transition vector).
TestVector transition_pair(const TestVector& v1, const TestVector& v2);

// The good machine's value at a transition fault's net before (pre) and after
// (post) the edge: STR (SA0) 0 -> 1, STF (SA1) 1 -> 0.
inline Logic transition_pre(FaultType type) {
  return type == FaultType::SA0 ? Logic::ZERO : Logic::ONE;
}
inline Logic transition_post(FaultType type) {
  return type == FaultType::SA0 ? Logic::ONE : Logic::ZERO;
}

}  // namespace faultflow
