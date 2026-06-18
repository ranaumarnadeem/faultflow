#include <catch2/catch_test_macros.hpp>

#include <limits>
#include <map>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

SatSolveOptions unlimited_solve_options() {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  return options;
}

TestVector vector_from_map(const ParsedGraph& pg,
                           const std::map<std::string, bool>& values) {
  TestVector vec;
  for (const auto& [name, value] : values) {
    vec.inputs[pg.net_id_by_name(name)] = value;
  }
  return vec;
}

CompactFault make_transition_fault(const CompiledSimGraph& cg, int yosys_net,
                                   FaultType type) {
  CompactFault f;
  f.net_index = cg.yosys_to_compiled.at(yosys_net);
  f.type = type;
  f.model = FaultModel::TRANSITION;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  f.exclusion = FaultExclusion::NONE;
  f.collapsed_into = std::numeric_limits<uint32_t>::max();
  return f;
}

// SAT-solve a transition fault, then confirm the emitted (V1,V2) is genuinely
// detecting through BOTH the golden oracle and the bit-parallel engine.
void require_solved_and_verified(const ParsedGraph& pg,
                                 const CompiledSimGraph& cg,
                                 const CompactFault& fault) {
  const auto pis = ordered_pis(pg, cg);
  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;
  REQUIRE(solve_transition_fault(cg, pis, fault, unlimited_solve_options(), v1,
                                 v2) == SatSolveResult::SAT);

  const TestVector tv1 = vector_from_map(pg, v1);
  const TestVector tv2 = vector_from_map(pg, v2);

  GoldenRefSim golden;
  BitParallelSim parallel;
  REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
  REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
}

}  // namespace

// ---------------------------------------------------------------------------
// STR / STF detection: solve -> verify parity
// ---------------------------------------------------------------------------

TEST_CASE("solve_transition_fault: STR on tiny_and2 output verifies",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  require_solved_and_verified(
      pg, cg, make_transition_fault(cg, pg.net_id_by_name("Y"), FaultType::SA0));
}

TEST_CASE("solve_transition_fault: STF on tiny_and2 output verifies",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  require_solved_and_verified(
      pg, cg, make_transition_fault(cg, pg.net_id_by_name("Y"), FaultType::SA1));
}

TEST_CASE("solve_transition_fault: STR/STF on tiny_chain output verifies",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_chain.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_chain.json");
  require_solved_and_verified(
      pg, cg, make_transition_fault(cg, pg.net_id_by_name("Y"), FaultType::SA0));
  require_solved_and_verified(
      pg, cg, make_transition_fault(cg, pg.net_id_by_name("Y"), FaultType::SA1));
}

// ---------------------------------------------------------------------------
// Redundancy: a constant net cannot transition -> UNSAT for both STR and STF
// ---------------------------------------------------------------------------

TEST_CASE("solve_transition_fault: constant output is redundant (UNSAT)",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_const.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_const.json");
  const auto pis = ordered_pis(pg, cg);

  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;

  // Y0 is a constant net: it can never make a 0->1 or 1->0 edge, so no
  // transition test exists. UNSAT = redundant for the transition model.
  const CompactFault str =
      make_transition_fault(cg, pg.net_id_by_name("Y0"), FaultType::SA0);
  REQUIRE(solve_transition_fault(cg, pis, str, unlimited_solve_options(), v1,
                                 v2) == SatSolveResult::UNSAT);

  const CompactFault stf =
      make_transition_fault(cg, pg.net_id_by_name("Y0"), FaultType::SA1);
  REQUIRE(solve_transition_fault(cg, pis, stf, unlimited_solve_options(), v1,
                                 v2) == SatSolveResult::UNSAT);
}

// ---------------------------------------------------------------------------
// Exhaustive solve -> verify parity over every enumerated transition fault
// ---------------------------------------------------------------------------

TEST_CASE("solve_transition_fault: every SAT vector verifies on tiny_chain",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_chain.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_chain.json");
  const NormalizedGraph ng = test::load_normalized("tiny_chain.json");
  const auto pis = ordered_pis(pg, cg);
  const auto faults = enumerate_transition_faults(ng, cg);

  GoldenRefSim golden;
  BitParallelSim parallel;
  int sat_count = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) continue;

    std::map<std::string, bool> v1;
    std::map<std::string, bool> v2;
    const SatSolveResult r =
        solve_transition_fault(cg, pis, fault, unlimited_solve_options(), v1, v2);
    if (r != SatSolveResult::SAT) continue;
    ++sat_count;

    const TestVector tv1 = vector_from_map(pg, v1);
    const TestVector tv2 = vector_from_map(pg, v2);
    // Every SAT-derived vector must be a genuine detection in both engines.
    REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
    REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
  }
  // Sanity: the fixture is testable, so at least some faults SAT.
  REQUIRE(sat_count > 0);
}

// ---------------------------------------------------------------------------
// Blocking a launch/capture pair forces a different vector or UNSAT
// ---------------------------------------------------------------------------

TEST_CASE("solve_transition_fault: blocked pair forces different or UNSAT",
          "[transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto pis = ordered_pis(pg, cg);

  const CompactFault fault =
      make_transition_fault(cg, pg.net_id_by_name("Y"), FaultType::SA0);

  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;
  REQUIRE(solve_transition_fault(cg, pis, fault, unlimited_solve_options(), v1,
                                 v2) == SatSolveResult::SAT);

  // Blocking the emitted launch||capture pair must yield either a genuinely
  // different pair or UNSAT — never the same pair again. (STR at AND2 output
  // admits several launch vectors, e.g. 00/01/10 -> Y=0, so a retry can still
  // SAT with a different launch.)
  SatSolveOptions blocked = unlimited_solve_options();
  blocked.blocked_patterns = {transition_pattern_key(v1, v2, pis)};
  std::map<std::string, bool> v1b;
  std::map<std::string, bool> v2b;
  const SatSolveResult retry =
      solve_transition_fault(cg, pis, fault, blocked, v1b, v2b);
  if (retry == SatSolveResult::SAT) {
    REQUIRE(transition_pattern_key(v1b, v2b, pis) !=
            transition_pattern_key(v1, v2, pis));
    // The new pair must also still be a genuine detection.
    GoldenRefSim golden;
    REQUIRE(golden.simulate_transition_fault(cg, vector_from_map(pg, v1b),
                                             vector_from_map(pg, v2b), fault));
  } else {
    REQUIRE(retry == SatSolveResult::UNSAT);
  }
}
