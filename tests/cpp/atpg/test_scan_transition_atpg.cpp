#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "atpg/progressive_atpg.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

// Scan launch-on-capture (LOC) two-frame transition ATPG, exercised on a
// hand-built PRE-REDUCED scan ATPG view (tiny_scan_view_loc.json):
//   real PI:      A
//   pseudo-PIs:   __ppi_ff0 (ff0 Q), __ppi_ff1 (ff1 Q)
//   pseudo-POs:   __ppo_ff0 = A XOR __ppi_ff0, __ppo_ff1 = __ppi_ff1
//   real PO:      Y = __ppi_ff0
// LOC couples each FF's capture-frame PPI to its launch-frame PPO; real PIs are
// held launch->capture. So ff0's Q can transition (when A=1) but ff1's Q holds.

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

// The two scan-FF LOC couples for tiny_scan_view_loc, as compiled indices.
std::vector<LocCouple> view_couples(const ParsedGraph& pg,
                                    const CompiledSimGraph& cg) {
  auto cidx = [&](const std::string& n) {
    return cg.yosys_to_compiled.at(pg.net_id_by_name(n));
  };
  return {LocCouple{cidx("__ppi_ff0"), cidx("__ppo_ff0")},
          LocCouple{cidx("__ppi_ff1"), cidx("__ppo_ff1")}};
}

// Real PIs held launch->capture (everything but the pseudo-PIs).
std::vector<uint32_t> view_held_pis(const ParsedGraph& pg,
                                    const CompiledSimGraph& cg) {
  return {cg.yosys_to_compiled.at(pg.net_id_by_name("A"))};
}

// SAT-solve a scan transition, confirm the emitted (V1,V2) is a genuine
// detection through BOTH oracles, and confirm the LOC coupling held: every
// capture-frame PPI equals the launch-frame PPO fault-free value.
void require_scan_solved_and_verified(const ParsedGraph& pg,
                                      const CompiledSimGraph& cg,
                                      const CompactFault& fault) {
  const auto pis = ordered_pis(pg, cg);
  const auto couples = view_couples(pg, cg);
  const auto held = view_held_pis(pg, cg);

  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;
  REQUIRE(solve_scan_transition_fault(cg, pis, couples, held, fault,
                                      unlimited_solve_options(), v1,
                                      v2) == SatSolveResult::SAT);

  const TestVector tv1 = vector_from_map(pg, v1);
  const TestVector tv2 = vector_from_map(pg, v2);

  GoldenRefSim golden;
  BitParallelSim parallel;
  REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
  REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));

  // LOC coupling: capture PPI == launch-frame PPO (the FF next-state).
  const auto launch_ff = golden.simulate_fault_free(cg, tv1);
  const std::vector<std::pair<std::string, std::string>> couple_names = {
      {"__ppi_ff0", "__ppo_ff0"}, {"__ppi_ff1", "__ppo_ff1"}};
  for (const auto& [ppi_name, ppo_name] : couple_names) {
    REQUIRE(v2.at(ppi_name) == launch_ff.at(pg.net_id_by_name(ppo_name)));
  }
  // Real PIs held: V2 == V1 on every PI this fixture's `held` list covers.
  // Matched by compiled index (not a hardcoded name) so this stays correct
  // whether a real PI is exposed as one bare-name PI (single-bit port) or
  // several bracket-indexed ones (multi-bit port).
  for (const auto& pi : pis) {
    if (std::find(held.begin(), held.end(), pi.compiled) != held.end()) {
      REQUIRE(v2.at(pi.name) == v1.at(pi.name));
    }
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// SAT: a Q-stem transition on a FF whose next-state can toggle (ff0)
// ---------------------------------------------------------------------------

TEST_CASE("scan LOC: STR on ff0 Q-stem verifies", "[scan_transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  require_scan_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff0"),
                            FaultType::SA0));
}

TEST_CASE("scan LOC: STF on ff0 Q-stem verifies", "[scan_transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  require_scan_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff0"),
                            FaultType::SA1));
}

// ---------------------------------------------------------------------------
// UNSAT: a Q-stem transition on a FF that holds (ff1: D=Q) is redundant
// ---------------------------------------------------------------------------

TEST_CASE("scan LOC: held FF Q-stem is redundant (UNSAT)",
          "[scan_transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  const auto pis = ordered_pis(pg, cg);
  const auto couples = view_couples(pg, cg);
  const auto held = view_held_pis(pg, cg);

  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;

  // ff1 holds (next-state == current-state under LOC), so its Q can never make
  // a 0->1 or 1->0 edge: no transition test exists. UNSAT = redundant.
  const CompactFault str = make_transition_fault(
      cg, pg.net_id_by_name("__ppi_ff1"), FaultType::SA0);
  REQUIRE(solve_scan_transition_fault(cg, pis, couples, held, str,
                                      unlimited_solve_options(), v1,
                                      v2) == SatSolveResult::UNSAT);

  const CompactFault stf = make_transition_fault(
      cg, pg.net_id_by_name("__ppi_ff1"), FaultType::SA1);
  REQUIRE(solve_scan_transition_fault(cg, pis, couples, held, stf,
                                      unlimited_solve_options(), v1,
                                      v2) == SatSolveResult::UNSAT);
}

// ---------------------------------------------------------------------------
// Exhaustive solve -> verify parity over every enumerated scan transition fault
// ---------------------------------------------------------------------------

TEST_CASE("scan LOC: every SAT vector verifies (bit-parallel == golden)",
          "[scan_transition][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  const NormalizedGraph ng = test::load_normalized("tiny_scan_view_loc.json");
  const auto pis = ordered_pis(pg, cg);
  const auto couples = view_couples(pg, cg);
  const auto held = view_held_pis(pg, cg);
  const auto faults = enumerate_transition_faults(ng, cg);

  GoldenRefSim golden;
  BitParallelSim parallel;
  int sat_count = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) continue;

    std::map<std::string, bool> v1;
    std::map<std::string, bool> v2;
    const SatSolveResult r = solve_scan_transition_fault(
        cg, pis, couples, held, fault, unlimited_solve_options(), v1, v2);
    if (r != SatSolveResult::SAT) continue;
    ++sat_count;

    const TestVector tv1 = vector_from_map(pg, v1);
    const TestVector tv2 = vector_from_map(pg, v2);
    REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
    REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
  }
  REQUIRE(sat_count > 0);
}

// ---------------------------------------------------------------------------
// Regression: held_real_pis must hold every BIT of a multi-bit real PI
// ---------------------------------------------------------------------------

// Before this fix, a >1-bit top-level input port was skipped from the held
// set entirely (`bits.size() != 1`) -- not collapsed to its first bit like
// ordered_pis's analogous bug, just silently absent. That left every bit of
// such a port completely unconstrained between the launch and capture frames
// of a two-frame transition test: the SAT solver was free to pick DIFFERENT
// values for e.g. A[1] in V1 vs V2, a vector no real tester could ever apply
// (a held, non-scan-driven input cannot change value between the launch and
// capture edges of one at-speed capture window), and the golden-ref replay
// had no independent way to reject it -- a soundness gap that could inflate
// reported transition coverage with physically invalid patterns. This same
// helper is shared by both the LOC (build_scan_loc_view) and LOS
// (solve_scan_los_transition_fault_for_db) views, so one test covers both.
TEST_CASE("held_real_pis holds every bit of a multi-bit real PI",
          "[scan_transition][atpg][pi-enum]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  const auto held = held_real_pis(pg, cg);

  std::vector<int> held_yosys_ids;
  held_yosys_ids.reserve(held.size());
  for (uint32_t compiled : held) {
    held_yosys_ids.push_back(cg.compiled_to_yosys.at(compiled));
  }
  std::sort(held_yosys_ids.begin(), held_yosys_ids.end());
  // Both bits of A (yosys net ids 2 and 8) must be held. Before the fix,
  // held_real_pis returned {} for A entirely -- the whole port was skipped.
  REQUIRE(held_yosys_ids == std::vector<int>{2, 8});
}
