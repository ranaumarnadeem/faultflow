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
};

XDetection classify_detection(const CompiledSimGraph& cg,
                              const std::vector<XSnapshot>& good,
                              const std::vector<XSnapshot>& faulty);

}  // namespace faultflow
