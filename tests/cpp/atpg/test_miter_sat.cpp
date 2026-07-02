#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <map>
#include <vector>

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

// Regression for the "blocked pattern length mismatch" crash: a multi-bit top
// module port (A[1:0], 2 bits) was silently DROPPED from ordered_pis (it
// required bits.size()==1), while the Python-side PI-name list used to build
// blocked_patterns bit-strings (faultflow/runner/runner.py's `_port_names`)
// counts the port once regardless of width, matching how
// ParsedGraph::net_id_by_name resolves a bare port name to its FIRST bit. The
// two enumerations then disagreed in length (Python: 1 entry for A; C++: 0),
// and any re-solve of a fault with an already-blocked pattern threw.
// ordered_pis must expose exactly one PI per input PORT (via its first bit,
// the same net_id_by_name already uses elsewhere for a bus PI), never zero.
TEST_CASE("ordered_pis includes a multi-bit input port via its first bit",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);

  // One PI per port (A, B, C) == 3 PIs. Before the fix, A was skipped
  // entirely (bits.size() == 2 != 1), leaving only B and C (2 PIs) --
  // desyncing this count from the Python-side PI-name list, which always
  // counts A once regardless of width.
  REQUIRE(pis.size() == 3);

  std::vector<int> yosys_ids;
  yosys_ids.reserve(pis.size());
  for (const auto& pi : pis) {
    yosys_ids.push_back(pi.yosys_id);
  }
  std::sort(yosys_ids.begin(), yosys_ids.end());
  // A resolves to its first bit (yosys net id 2, matching
  // ParsedGraph::net_id_by_name("A")), plus B (4) and C (5).
  REQUIRE(yosys_ids == std::vector<int>{2, 4, 5});
}

// End-to-end reproduction of the actual crash: solve a fault, block the
// returned candidate's pattern (as the progressive-ATPG loop does on a
// rejected/duplicate candidate), then re-solve the SAME fault with that
// blocked pattern. Before the fix, pattern_key(...) over the (buggy, A-less)
// `pis` produced a length-2 key while a caller sizing its blocked-pattern
// list off the Python-side PI-name list (which counts A once, like every
// other port) would pass a length-3 key -- exactly the "blocked pattern
// length mismatch" crash. After the fix both sides agree on 3, so
// re-solving with a blocked pattern sized off the full PI list must not
// throw.
TEST_CASE("Re-solving with a full-width blocked pattern does not throw on a "
          "multi-bit-PI design",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);
  REQUIRE(pis.size() == 3);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y1"));
  fault.type = FaultType::SA0;

  std::map<std::string, bool> first;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(),
                               first) == SatSolveResult::SAT);

  // Block the just-found pattern (full pis.size()-width key, as the caller
  // that persists blocked_patterns does) and re-solve the same fault.
  SatSolveOptions blocked = unlimited_solve_options();
  const std::string key = pattern_key(first, pis);
  REQUIRE(key.size() == pis.size());
  blocked.blocked_patterns = {key};
  std::map<std::string, bool> second;
  REQUIRE_NOTHROW(solve_stuck_at_fault(cg, pis, fault, blocked, second));
}
