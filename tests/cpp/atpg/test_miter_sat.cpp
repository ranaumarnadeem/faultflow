#include <catch2/catch_test_macros.hpp>

#include <map>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

TestVector vector_from_map(const ParsedGraph& pg,
                           const std::map<std::string, bool>& values) {
  TestVector vec;
  for (const auto& [name, value] : values) {
    vec.inputs[pg.net_id_by_name(name)] = value;
  }
  return vec;
}

SatSolveOptions unlimited_solve_options() {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  return options;
}

}  // namespace

TEST_CASE("Miter SAT returns detectable vector for tiny_inv output SA0",
          "[atpg][miter]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const auto pis = ordered_pis(pg, cg);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  fault.type = FaultType::SA0;

  std::map<std::string, bool> candidate;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(),
                               candidate) == SatSolveResult::SAT);

  GoldenRefSim golden;
  BitParallelSim parallel;
  const TestVector vec = vector_from_map(pg, candidate);
  REQUIRE(golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                             golden.simulate_with_fault(cg, vec, fault)));
  REQUIRE(parallel.simulate_single_fault(cg, vec, fault));
}

TEST_CASE("Miter UNSAT marks redundant const-zero output SA0", "[atpg][miter]") {
  const ParsedGraph pg = test::load_parsed("tiny_const.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_const.json");
  const auto pis = ordered_pis(pg, cg);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y0"));
  fault.type = FaultType::SA0;

  std::map<std::string, bool> candidate;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(),
                               candidate) == SatSolveResult::UNSAT);
}

// tdd.md RULE 9: "conflict_limit=0 on tiny_inv => TIMEOUT" was wrong — tiny_inv
// SATs via propagation only. tiny_chain requires search; conflict_limit=0 interrupts.

TEST_CASE("solve_stuck_at_fault returns TIMEOUT when CaDiCaL interrupts",
          "[atpg][miter]") {
  const ParsedGraph pg = test::load_parsed("tiny_chain.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_chain.json");
  const auto pis = ordered_pis(pg, cg);
  REQUIRE_FALSE(pis.empty());

  // Probe showed tiny_chain net_index=0 (first PI branch) needs search under
  // conflict_limit=0; output Y still propagates without conflicts.
  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("A"));
  fault.type = FaultType::SA0;

  SatSolveOptions options;
  options.conflict_limit = 0;
  options.sat_timeout_seconds = 0;

  std::map<std::string, bool> candidate;
  const SatSolveResult result =
      solve_stuck_at_fault(cg, pis, fault, options, candidate);
  REQUIRE(result == SatSolveResult::TIMEOUT);
}

TEST_CASE("Blocked pattern forces different SAT assignment or UNSAT",
          "[atpg][miter]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const auto pis = ordered_pis(pg, cg);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  fault.type = FaultType::SA0;

  std::map<std::string, bool> first;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(), first) ==
          SatSolveResult::SAT);

  GoldenRefSim golden;
  const TestVector first_vec = vector_from_map(pg, first);
  const bool first_detects =
      golden.is_detected(cg, golden.simulate_fault_free(cg, first_vec),
                         golden.simulate_with_fault(cg, first_vec, fault));
  REQUIRE(first_detects);

  SatSolveOptions blocked = unlimited_solve_options();
  blocked.blocked_patterns = {pattern_key(first, pis)};
  std::map<std::string, bool> second;
  const SatSolveResult retry =
      solve_stuck_at_fault(cg, pis, fault, blocked, second);
  REQUIRE(retry == SatSolveResult::UNSAT);
}

TEST_CASE("Non-detecting candidate does not imply fault detected", "[atpg][miter]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  fault.type = FaultType::SA1;
  fault.status = FaultStatus::UNDETECTED;

  TestVector vec;
  vec.inputs[pg.net_id_by_name("A")] = false;

  GoldenRefSim golden;
  REQUIRE_FALSE(golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                                  golden.simulate_with_fault(cg, vec, fault)));
  REQUIRE(fault.status == FaultStatus::UNDETECTED);
}

TEST_CASE("map_cadical_result mapping is pinned", "[atpg][miter]") {
  REQUIRE(map_cadical_result(10, false) == SatSolveResult::SAT);
  REQUIRE(map_cadical_result(20, false) == SatSolveResult::UNSAT);
  REQUIRE(map_cadical_result(0, true) == SatSolveResult::TIMEOUT);
  REQUIRE(map_cadical_result(0, false) == SatSolveResult::UNKNOWN);
}
