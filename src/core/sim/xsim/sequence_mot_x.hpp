#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "sim/xsim/logic.hpp"
#include "sim/xsim/sequence_grade_x.hpp"

namespace faultflow {

// Detection over multiple observation times (Pomeranz and Reddy, 1992) for a
// sequence graded with unknowns. grade_sequence_faults_x credits a fault only
// where one sample shows a 0 against a 1 for every completion of the unknowns at
// once; here a fault is detected when every completion -- each flop's and
// clock-gate latch's power-up value, each input unknown in a cycle, each x bit's
// value wherever it is read -- shows the good machine's known value inverted at
// some compared bit, perhaps a different one for each. That is what a tester
// comparing every care bit sees: whatever the faulty chip powers up to, the test
// fails.
//
// Exact, not a bound: the faulty machine is simulated once with every unknown a
// SAT variable (each net a constant or a literal over them, gates structurally
// hashed), and a fault is detected when no assignment matches the good machine
// at every compared bit (CaDiCaL proves it UNSAT). Only the good machine's known
// bits are compared, as a tester masks the rest.
struct XMotRequest {
  // The sequence. sequence.base.sample marks the sampled cycles, observe_outputs
  // the outputs, faults the faults to decide.
  XSequenceGradeRequest sequence;
  // Per sampled cycle, per observe_outputs entry: is it compared there. Empty:
  // every one.
  std::vector<std::vector<bool>> compare;
  // Per fault, the wall-clock seconds (<= 0: no limit) and the SAT variables
  // allowed; past either the fault is UNDECIDED.
  double seconds_per_fault = 60.0;
  int64_t max_variables = 4000000;
  // Report, per ESCAPES fault, a flop power-up state it escapes from.
  bool witness = false;
};

enum class MotOutcome : uint8_t {
  ESCAPES,    // some completion of the unknowns matches every compared bit
  DETECTED,   // none does
  UNDECIDED,  // a budget ran out, or the graph has a combinational loop or a bus
};

struct XMotResult {
  // The good machine's observed values per sampled cycle (0, 1 or X), as
  // XSequenceGradeResult::golden.
  std::vector<std::vector<Logic>> golden;
  std::vector<MotOutcome> outcome;
  // DETECTED: the first sample by which every completion has shown a
  // difference; -1 otherwise.
  std::vector<int32_t> detected_by;
  // With request.witness, per ESCAPES fault, a power-up value per flop
  // (CompiledSimGraph::ff_nodes order; -1 where it isn't unknown) of an escaping
  // completion; empty for the others.
  std::vector<std::vector<int8_t>> witness;
};

// Faults in parallel over sim_threads, one SAT solver each.
XMotResult grade_sequence_faults_mot_x(const std::string& json_path,
                                       const std::string& cell_map_path,
                                       const XMotRequest& request,
                                       const std::string& unsupported_policy,
                                       const std::vector<std::string>& blackbox_instances,
                                       int sim_threads);

}  // namespace faultflow
