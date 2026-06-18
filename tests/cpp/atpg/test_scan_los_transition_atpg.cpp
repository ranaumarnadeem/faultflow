#include <catch2/catch_test_macros.hpp>

#include <limits>
#include <map>
#include <string>
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

// Scan launch-on-shift (LOS) two-frame transition ATPG, on a hand-built reduced
// scan view (tiny_scan_view_los.json). Chain 0 = [ff0(head), ff1]; chain 1 =
// [ff2(head, alone)]. Under LOS the launch is the last scan shift, so:
//   capture PPI[ff1] == launch PPI[ff0]   (its chain predecessor's loaded value)
//   capture PPI[ff0] / PPI[ff2] are FREE  (the fresh scan-in bit at each head)
// Real PI A is held launch->capture.

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

uint32_t cidx(const ParsedGraph& pg, const CompiledSimGraph& cg,
              const std::string& net) {
  return cg.yosys_to_compiled.at(pg.net_id_by_name(net));
}

// LOS couples: ff1's predecessor is ff0 (chain 0). ff0 and ff2 are chain heads.
std::vector<LosCouple> view_couples(const ParsedGraph& pg,
                                    const CompiledSimGraph& cg) {
  return {LosCouple{cidx(pg, cg, "__ppi_ff1"), cidx(pg, cg, "__ppi_ff0")}};
}

std::vector<uint32_t> view_heads(const ParsedGraph& pg,
                                 const CompiledSimGraph& cg) {
  return {cidx(pg, cg, "__ppi_ff0"), cidx(pg, cg, "__ppi_ff2")};
}

std::vector<uint32_t> view_held(const ParsedGraph& pg,
                                const CompiledSimGraph& cg) {
  return {cidx(pg, cg, "A")};
}

void require_los_solved_and_verified(const ParsedGraph& pg,
                                     const CompiledSimGraph& cg,
                                     const CompactFault& fault) {
  const auto pis = ordered_pis(pg, cg);
  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;
  REQUIRE(solve_scan_los_transition_fault(
              cg, pis, view_couples(pg, cg), view_heads(pg, cg),
              view_held(pg, cg), fault, unlimited_solve_options(), v1, v2) ==
          SatSolveResult::SAT);

  const TestVector tv1 = vector_from_map(pg, v1);
  const TestVector tv2 = vector_from_map(pg, v2);

  GoldenRefSim golden;
  BitParallelSim parallel;
  REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
  REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));

  // LOS shift coupling: ff1's capture PPI == ff0's LAUNCH PPI (predecessor).
  REQUIRE(v2.at("__ppi_ff1") == v1.at("__ppi_ff0"));
  // Real PI A held.
  REQUIRE(v2.at("A") == v1.at("A"));
}

}  // namespace

// ---------------------------------------------------------------------------
// Non-head FF: the predecessor's loaded value drives the launched transition
// ---------------------------------------------------------------------------

TEST_CASE("scan LOS: STR on non-head ff1 Q-stem verifies",
          "[scan_los][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  require_los_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff1"),
                            FaultType::SA0));
}

TEST_CASE("scan LOS: STF on non-head ff1 Q-stem verifies",
          "[scan_los][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  require_los_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff1"),
                            FaultType::SA1));
}

// ---------------------------------------------------------------------------
// Chain head: the fresh scan-in bit supplies the launched edge (free var)
// ---------------------------------------------------------------------------

TEST_CASE("scan LOS: STR on chain-head ff0 Q-stem verifies (free scan-in)",
          "[scan_los][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  require_los_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff0"),
                            FaultType::SA0));
}

TEST_CASE("scan LOS: single-FF chain head ff2 Q-stem verifies",
          "[scan_los][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  require_los_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff2"),
                            FaultType::SA0));
  require_los_solved_and_verified(
      pg, cg,
      make_transition_fault(cg, pg.net_id_by_name("__ppi_ff2"),
                            FaultType::SA1));
}

// ---------------------------------------------------------------------------
// Exhaustive solve -> verify parity over every enumerated transition fault
// ---------------------------------------------------------------------------

TEST_CASE("scan LOS: every SAT vector verifies (bit-parallel == golden)",
          "[scan_los][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  const NormalizedGraph ng = test::load_normalized("tiny_scan_view_los.json");
  const auto pis = ordered_pis(pg, cg);
  const auto faults = enumerate_transition_faults(ng, cg);

  GoldenRefSim golden;
  BitParallelSim parallel;
  int sat_count = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) continue;

    std::map<std::string, bool> v1;
    std::map<std::string, bool> v2;
    const SatSolveResult r = solve_scan_los_transition_fault(
        cg, pis, view_couples(pg, cg), view_heads(pg, cg), view_held(pg, cg),
        fault, unlimited_solve_options(), v1, v2);
    if (r != SatSolveResult::SAT) continue;
    ++sat_count;

    const TestVector tv1 = vector_from_map(pg, v1);
    const TestVector tv2 = vector_from_map(pg, v2);
    REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
    REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
  }
  REQUIRE(sat_count > 0);
}
