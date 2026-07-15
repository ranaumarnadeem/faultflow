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

// Regression, part 1 of this bug's history: the "blocked pattern length
// mismatch" crash. A multi-bit top module port (A[1:0], 2 bits) was silently
// DROPPED from ordered_pis (it required bits.size()==1), while the
// Python-side PI-name list used to build blocked_patterns bit-strings
// (faultflow/runner/runner.py's `_port_names`) counted the port once
// regardless of width. The fix at the time -- "take the first bit, so every
// port contributes exactly one PI" -- fixed the crash but left bits 1..N-1
// permanently unaddressable: a fault detectable only via a non-front bit
// (e.g. A[1]) could be solved correctly inside the CNF (every net gets its
// own free variable, independent of `pis`) but the solved value could never
// be read back into the returned candidate map, silently defaulting to 0 on
// replay -- the `tier_a_reduced_mismatch` bug (see the two TEST_CASEs below
// this one for the direct regression). ordered_pis now expands a multi-bit
// port into one PI per BIT, named "<port>[<i>]"; a single-bit port keeps its
// exact prior (bare-name) behavior unchanged.
TEST_CASE("ordered_pis expands a multi-bit input port into one PI per bit",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);

  // One PI per bit: A[0], A[1], B, C == 4 PIs. Before this fix, A collapsed
  // to a single PI (its first bit only), giving 3.
  REQUIRE(pis.size() == 4);

  std::vector<int> yosys_ids;
  yosys_ids.reserve(pis.size());
  for (const auto& pi : pis) {
    yosys_ids.push_back(pi.yosys_id);
  }
  std::sort(yosys_ids.begin(), yosys_ids.end());
  // A's both bits (2, 3), plus B (4) and C (5) -- every underlying net now
  // has its own addressable PI.
  REQUIRE(yosys_ids == std::vector<int>{2, 3, 4, 5});

  std::vector<std::string> names;
  names.reserve(pis.size());
  for (const auto& pi : pis) {
    names.push_back(pi.name);
  }
  std::sort(names.begin(), names.end());
  REQUIRE(names == std::vector<std::string>{"A[0]", "A[1]", "B", "C"});
}

// End-to-end reproduction of the original crash this fixture was built for:
// solve a fault, block the returned candidate's pattern (as the
// progressive-ATPG loop does on a rejected/duplicate candidate), then
// re-solve the SAME fault with that blocked pattern. Both the C++ `pis` list
// and the Python-side PI-name list must always agree on length (now 4, one
// per bit) or this throws "blocked pattern length mismatch".
TEST_CASE("Re-solving with a full-width blocked pattern does not throw on a "
          "multi-bit-PI design",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);
  REQUIRE(pis.size() == 4);

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

// Regression, part 2: the actual `tier_a_reduced_mismatch` bug. Y3 = INV(A[1])
// is stuck-at-1-detectable ONLY via A[1]=1 -- a non-front bit of the 2-bit
// port A. Before this fix, ordered_pis() enumerated exactly one PI for "A"
// (resolving only to A[0]/net 2), so CaDiCaL's internally-correct requirement
// that A[1]=1 was solved inside the CNF (every net gets its own free
// variable there, independent of `pis`) but never read back into the
// returned candidate map. Replaying that candidate then defaulted A[1] to 0
// (GoldenRefSim defaults any PI net absent from TestVector::inputs to
// false) -- exactly canceling the fault's own forced value, so the replay
// disagreed the fault was detected even though SAT had just proved it was.
// This is the precise mechanism behind every real-world `tier_a_reduced_mismatch`
// rejection: SAT keeps finding genuinely fresh witnesses, and every one of
// them fails re-verification, deterministically, forever.
TEST_CASE("Fault detectable only via a non-front PI bit survives replay",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y3"));
  fault.type = FaultType::SA1;

  std::map<std::string, bool> candidate;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(),
                               candidate) == SatSolveResult::SAT);

  GoldenRefSim golden;
  const TestVector vec = vector_from_map(pg, candidate);
  REQUIRE(golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                             golden.simulate_with_fault(cg, vec, fault)));
}

// pattern_key must discriminate two candidates that differ ONLY in a
// non-front PI bit. Before this fix, `pis` had no entry naming A[1] at all,
// so both of these hand-built candidates collapsed to the identical key
// (A[1]'s intended value simply had nowhere to go) -- exactly why "genuinely
// distinct SAT witnesses, every one rejected" was observed empirically
// rather than an ordinary duplicate-pattern collision (which the earlier
// `rejected_patterns` fix already handles correctly).
TEST_CASE("pattern_key discriminates candidates differing only in a "
          "non-front PI bit",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);

  const std::map<std::string, bool> v1{
      {"A[0]", false}, {"A[1]", false}, {"B", false}, {"C", false}};
  const std::map<std::string, bool> v2{
      {"A[0]", false}, {"A[1]", true}, {"B", false}, {"C", false}};

  REQUIRE(pattern_key(v1, pis) != pattern_key(v2, pis));
}

// net_id_by_name's new bracket-index resolution: additive only. Bare names
// (including a genuinely single-bit port) resolve exactly as before;
// "<port>[<i>]" now resolves to that specific bit; out-of-range indices and
// unknown base ports still throw, same as any other unresolvable name.
TEST_CASE("net_id_by_name resolves bracket-indexed multi-bit port names",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");

  REQUIRE(pg.net_id_by_name("A[0]") == 2);
  REQUIRE(pg.net_id_by_name("A[1]") == 3);
  // Bare-name resolution is unchanged: still the first bit.
  REQUIRE(pg.net_id_by_name("A") == 2);
  // A genuinely single-bit port is completely unaffected.
  REQUIRE(pg.net_id_by_name("B") == 4);

  REQUIRE_THROWS(pg.net_id_by_name("A[2]"));     // out of range
  REQUIRE_THROWS(pg.net_id_by_name("A[x]"));     // malformed index
  REQUIRE_THROWS(pg.net_id_by_name("Nope[0]"));  // unknown base port
}

// Resume-safety companion (second line of defense, independent of the Python
// fingerprint gate): a `blocked_patterns` entry sized for the OLD (pre-fix,
// one-PI-per-port) enumeration must still be rejected cleanly if it ever
// reaches this layer directly, not silently misinterpreted bit-by-bit
// against the new (longer, one-PI-per-bit) `pis` list.
TEST_CASE("Wrong-length blocked pattern throws cleanly, not silently",
          "[atpg][miter][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_bus_pi.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_bus_pi.json");
  const auto pis = ordered_pis(pg, cg);
  REQUIRE(pis.size() == 4);

  CompactFault fault;
  fault.net_index = cg.yosys_to_compiled.at(pg.net_id_by_name("Y1"));
  fault.type = FaultType::SA0;

  SatSolveOptions stale = unlimited_solve_options();
  // Pre-fix length: one entry per PORT (A, B, C), not per bit.
  stale.blocked_patterns = {std::string(3, '0')};
  std::map<std::string, bool> candidate;
  REQUIRE_THROWS(solve_stuck_at_fault(cg, pis, fault, stale, candidate));
}
