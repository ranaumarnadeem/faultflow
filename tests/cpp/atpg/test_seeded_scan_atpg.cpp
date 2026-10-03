#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
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

// Scan ATPG through a compression decompressor: every scan cell (PPI) is loaded
// with the XOR of some bits of the decompressor's seed
// (faultflow/scan/ring_generator.py::care_bit_rows), so only the loads the seed
// space spans are applicable. Each test checks the seeded solver's verdict
// against brute force over every seed and every real-PI value -- an oracle that
// shares nothing with the CNF -- and every SAT vector against the golden and
// bit-parallel simulators and against the seed it must come from.

namespace {

SatSolveOptions unlimited_solve_options() {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  return options;
}

uint32_t cidx(const ParsedGraph& pg, const CompiledSimGraph& cg,
              const std::string& net) {
  return cg.yosys_to_compiled.at(pg.net_id_by_name(net));
}

TestVector vector_from_map(const ParsedGraph& pg,
                           const std::map<std::string, bool>& values) {
  TestVector vec;
  for (const auto& [name, value] : values) {
    vec.inputs[pg.net_id_by_name(name)] = value;
  }
  return vec;
}

// The value the decompressor loads for a cell whose row is `bits`.
bool seeded_value(const std::vector<int>& bits, uint32_t seed) {
  bool value = false;
  for (int bit : bits) {
    value ^= ((seed >> bit) & 1U) != 0;
  }
  return value;
}

// A name -> seed-bit row table, as a SatSolveOptions entry list.
std::vector<SeededInput> seeded(
    const ParsedGraph& pg, const CompiledSimGraph& cg,
    const std::vector<std::pair<std::string, std::vector<int>>>& rows) {
  std::vector<SeededInput> out;
  for (const auto& [name, bits] : rows) {
    out.push_back(SeededInput{cidx(pg, cg, name), bits});
  }
  return out;
}

// True iff some seed in [0, 2^width) loads every named cell with its value in
// `values` (the cells of `rows`).
bool loads_from_some_seed(
    const std::vector<std::pair<std::string, std::vector<int>>>& rows,
    const std::map<std::string, bool>& values, int width) {
  for (uint32_t seed = 0; seed < (1U << width); ++seed) {
    bool all = true;
    for (const auto& [name, bits] : rows) {
      if (values.at(name) != seeded_value(bits, seed)) {
        all = false;
        break;
      }
    }
    if (all) {
      return true;
    }
  }
  return false;
}

CompactFault stuck_at(const CompiledSimGraph& cg, int yosys_net, FaultType type) {
  CompactFault f;
  f.net_index = cg.yosys_to_compiled.at(yosys_net);
  f.type = type;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  f.exclusion = FaultExclusion::NONE;
  f.collapsed_into = std::numeric_limits<uint32_t>::max();
  return f;
}

// tiny_scan_view_seeded: ff0 = s0, ff1 = s0 ^ s1, ff2 = s1.
const std::vector<std::pair<std::string, std::vector<int>>> kSeededRows = {
    {"__ppi_ff0", {0}}, {"__ppi_ff1", {0, 1}}, {"__ppi_ff2", {1}}};

SatSolveOptions seeded_options(const ParsedGraph& pg, const CompiledSimGraph& cg) {
  SatSolveOptions options = unlimited_solve_options();
  options.seed_width = 2;
  options.seeded_inputs = seeded(pg, cg, kSeededRows);
  return options;
}

// Brute force: does any seed and value of A detect `fault`?
bool stuck_at_detectable_through_seed(const ParsedGraph& pg,
                                      const CompiledSimGraph& cg,
                                      const CompactFault& fault) {
  const GoldenRefSim golden;
  for (uint32_t seed = 0; seed < 4; ++seed) {
    for (int a = 0; a < 2; ++a) {
      std::map<std::string, bool> values = {{"A", a == 1}};
      for (const auto& [name, bits] : kSeededRows) {
        values[name] = seeded_value(bits, seed);
      }
      const TestVector vec = vector_from_map(pg, values);
      if (golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                             golden.simulate_with_fault(cg, vec, fault))) {
        return true;
      }
    }
  }
  return false;
}

}  // namespace

// ---------------------------------------------------------------------------
// Stuck-at
// ---------------------------------------------------------------------------

TEST_CASE("seeded stuck-at: a fault no seed can test is UNSAT, free it is SAT",
          "[atpg][seeded]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_seeded.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_seeded.json");
  const auto pis = ordered_pis(pg, cg);
  // Y stuck-at-0 needs ff0 = ff1 = ff2 = 1, but every load has ff1 = ff0 ^ ff2.
  const CompactFault fault = stuck_at(cg, pg.net_id_by_name("Y"), FaultType::SA0);

  std::map<std::string, bool> vector;
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, seeded_options(pg, cg), vector) ==
          SatSolveResult::UNSAT);
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(),
                               vector) == SatSolveResult::SAT);
}

TEST_CASE("seeded stuck-at: the incremental and mode-aware solvers honour the seed",
          "[atpg][seeded]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_seeded.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_seeded.json");
  const auto pis = ordered_pis(pg, cg);
  const CompactFault fault = stuck_at(cg, pg.net_id_by_name("Y"), FaultType::SA0);

  std::map<std::string, bool> vector;
  REQUIRE(solve_stuck_at_fault_incremental(cg, pis, fault, seeded_options(pg, cg),
                                           vector) == SatSolveResult::UNSAT);
  REQUIRE(solve_stuck_at_fault_incremental(cg, pis, fault,
                                           unlimited_solve_options(),
                                           vector) == SatSolveResult::SAT);
  // EXTEST on a view without wrapper cells observes every output, as
  // FUNCTIONAL does -- but through the mode-aware miter.
  const ModeConfig extest = build_mode_config(cg, TestMode::EXTEST);
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, seeded_options(pg, cg), vector,
                               extest) == SatSolveResult::UNSAT);
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, unlimited_solve_options(), vector,
                               extest) == SatSolveResult::SAT);
}

TEST_CASE("seeded stuck-at: every verdict matches brute force over the seed space",
          "[atpg][seeded]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_seeded.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_seeded.json");
  const NormalizedGraph ng = test::load_normalized("tiny_scan_view_seeded.json");
  const auto pis = ordered_pis(pg, cg);
  const GoldenRefSim golden;
  const BitParallelSim parallel;
  int sat = 0;
  int unsat = 0;
  for (bool incremental : {false, true}) {
    for (bool cone_restrict : {false, true}) {
      for (const CompactFault& fault : enumerate_faults(ng, cg)) {
        if (fault.exclusion != FaultExclusion::NONE) continue;
        SatSolveOptions options = seeded_options(pg, cg);
        options.cone_restrict = cone_restrict;
        std::map<std::string, bool> vector;
        const SatSolveResult result =
            incremental
                ? solve_stuck_at_fault_incremental(cg, pis, fault, options, vector)
                : solve_stuck_at_fault(cg, pis, fault, options, vector);
        REQUIRE(result != SatSolveResult::TIMEOUT);
        REQUIRE(result != SatSolveResult::UNKNOWN);
        REQUIRE((result == SatSolveResult::SAT) ==
                stuck_at_detectable_through_seed(pg, cg, fault));
        if (result != SatSolveResult::SAT) {
          ++unsat;
          continue;
        }
        ++sat;
        REQUIRE(loads_from_some_seed(kSeededRows, vector, 2));
        const TestVector vec = vector_from_map(pg, vector);
        REQUIRE(golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                                   golden.simulate_with_fault(cg, vec, fault)));
        REQUIRE(parallel.simulate_single_fault(cg, vec, fault));
      }
    }
  }
  REQUIRE(sat > 0);
  REQUIRE(unsat > 0);
}

TEST_CASE("seeded stuck-at: an empty row loads 0, a row repeating a bit cancels",
          "[atpg][seeded]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_seeded.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_seeded.json");
  const auto pis = ordered_pis(pg, cg);
  // __ppo_ff1 = ff1; its stuck-at-0 needs ff1 = 1.
  const CompactFault fault =
      stuck_at(cg, pg.net_id_by_name("__ppo_ff1"), FaultType::SA0);
  SatSolveOptions options = unlimited_solve_options();
  options.seed_width = 2;
  std::map<std::string, bool> vector;

  options.seeded_inputs = seeded(pg, cg, {{"__ppi_ff1", {}}});
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, options, vector) ==
          SatSolveResult::UNSAT);
  options.seeded_inputs = seeded(pg, cg, {{"__ppi_ff1", {1, 1}}});
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, options, vector) ==
          SatSolveResult::UNSAT);
  options.seeded_inputs = seeded(pg, cg, {{"__ppi_ff1", {0, 1, 1}}});
  REQUIRE(solve_stuck_at_fault(cg, pis, fault, options, vector) ==
          SatSolveResult::SAT);
  REQUIRE(vector.at("__ppi_ff1"));
}

TEST_CASE("seeded stuck-at: a seed bit outside the seed width throws",
          "[atpg][seeded]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_seeded.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_seeded.json");
  const auto pis = ordered_pis(pg, cg);
  const CompactFault fault = stuck_at(cg, pg.net_id_by_name("Y"), FaultType::SA1);
  SatSolveOptions options = unlimited_solve_options();
  options.seed_width = 2;
  options.seeded_inputs = seeded(pg, cg, {{"__ppi_ff0", {2}}});
  std::map<std::string, bool> vector;
  REQUIRE_THROWS_AS(solve_stuck_at_fault(cg, pis, fault, options, vector),
                    std::runtime_error);
  options.seeded_inputs = seeded(pg, cg, {{"__ppi_ff0", {-1}}});
  REQUIRE_THROWS_AS(solve_stuck_at_fault(cg, pis, fault, options, vector),
                    std::runtime_error);
}

// ---------------------------------------------------------------------------
// Launch-on-capture: the launch frame's cells come from the seed
// ---------------------------------------------------------------------------

namespace {

// tiny_scan_view_loc: __ppo_ff0 = A[0] ^ __ppi_ff0, __ppo_ff1 = __ppi_ff1,
// Y = __ppi_ff0, Y2 = A[1]. Loaded from a 1-bit seed: ff0 = ff1 = s0.
const std::vector<std::pair<std::string, std::vector<int>>> kLocRows = {
    {"__ppi_ff0", {0}}, {"__ppi_ff1", {0}}};

}  // namespace

TEST_CASE("seeded LOC: every verdict matches brute force over the seed space",
          "[atpg][seeded][scan_transition]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_loc.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_loc.json");
  const NormalizedGraph ng = test::load_normalized("tiny_scan_view_loc.json");
  const auto pis = ordered_pis(pg, cg);
  const std::vector<LocCouple> couples = {
      LocCouple{cidx(pg, cg, "__ppi_ff0"), cidx(pg, cg, "__ppo_ff0")},
      LocCouple{cidx(pg, cg, "__ppi_ff1"), cidx(pg, cg, "__ppo_ff1")}};
  std::vector<uint32_t> held;
  for (const AtpgPiInfo& pi : pis) {
    if (pi.name.rfind("__ppi_", 0) != 0) held.push_back(pi.compiled);
  }
  SatSolveOptions options = unlimited_solve_options();
  options.seed_width = 1;
  options.seeded_inputs = seeded(pg, cg, kLocRows);
  const GoldenRefSim golden;
  const BitParallelSim parallel;

  // The launch pair LOC applies for one seed and real-PI value: V2's cells
  // capture V1's next state, the real PIs hold.
  const auto loc_pair = [&](uint32_t seed, int a0, int a1) {
    std::map<std::string, bool> v1 = {{"A[0]", a0 == 1}, {"A[1]", a1 == 1}};
    for (const auto& [name, bits] : kLocRows) v1[name] = seeded_value(bits, seed);
    const auto next = golden.simulate_fault_free(cg, vector_from_map(pg, v1));
    std::map<std::string, bool> v2 = v1;
    v2["__ppi_ff0"] = next.at(pg.net_id_by_name("__ppo_ff0"));
    v2["__ppi_ff1"] = next.at(pg.net_id_by_name("__ppo_ff1"));
    return std::make_pair(v1, v2);
  };

  int sat = 0;
  int unsat = 0;
  for (const CompactFault& fault : enumerate_transition_faults(ng, cg)) {
    if (fault.exclusion != FaultExclusion::NONE) continue;
    bool detectable = false;
    for (uint32_t seed = 0; seed < 2 && !detectable; ++seed) {
      for (int a = 0; a < 4 && !detectable; ++a) {
        const auto [v1, v2] = loc_pair(seed, a & 1, (a >> 1) & 1);
        detectable = golden.simulate_transition_fault(
            cg, vector_from_map(pg, v1), vector_from_map(pg, v2), fault);
      }
    }
    std::map<std::string, bool> v1;
    std::map<std::string, bool> v2;
    const SatSolveResult result =
        solve_scan_transition_fault(cg, pis, couples, held, fault, options, v1, v2);
    REQUIRE((result == SatSolveResult::SAT) == detectable);
    if (result != SatSolveResult::SAT) {
      REQUIRE(result == SatSolveResult::UNSAT);
      ++unsat;
      continue;
    }
    ++sat;
    REQUIRE(loads_from_some_seed(kLocRows, v1, 1));
    const TestVector tv1 = vector_from_map(pg, v1);
    const TestVector tv2 = vector_from_map(pg, v2);
    REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
    REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
  }
  REQUIRE(sat > 0);
  REQUIRE(unsat > 0);
}

// ---------------------------------------------------------------------------
// Launch-on-shift: the loaded cells AND the launch shift's scan-in bits come
// from the seed (the decompressor keeps running into the launch shift)
// ---------------------------------------------------------------------------

namespace {

// tiny_scan_view_los: chain 0 = [ff0 (head), ff1], chain 1 = [ff2 (head)];
// Y = __ppi_ff1, __ppo_ff0 = A ^ __ppi_ff0, __ppo_ff1 = A ^ __ppi_ff1,
// __ppo_ff2 = __ppi_ff2. Loads: ff0 = s0, ff1 = s1, ff2 = s0 ^ s1. Launch-shift
// scan-in bits: chain 0 head (ff0) = s1, chain 1 head (ff2) = s0.
const std::vector<std::pair<std::string, std::vector<int>>> kLosLoadRows = {
    {"__ppi_ff0", {0}}, {"__ppi_ff1", {1}}, {"__ppi_ff2", {0, 1}}};
const std::vector<std::pair<std::string, std::vector<int>>> kLosHeadRows = {
    {"__ppi_ff0", {1}}, {"__ppi_ff2", {0}}};

}  // namespace

TEST_CASE("seeded LOS: every verdict matches brute force over the seed space",
          "[atpg][seeded][scan_los]") {
  const ParsedGraph pg = test::load_parsed("tiny_scan_view_los.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_view_los.json");
  const NormalizedGraph ng = test::load_normalized("tiny_scan_view_los.json");
  const auto pis = ordered_pis(pg, cg);
  const std::vector<LosCouple> couples = {
      LosCouple{cidx(pg, cg, "__ppi_ff1"), cidx(pg, cg, "__ppi_ff0")}};
  const std::vector<uint32_t> heads = {cidx(pg, cg, "__ppi_ff0"),
                                       cidx(pg, cg, "__ppi_ff2")};
  const std::vector<uint32_t> held = {cidx(pg, cg, "A")};
  SatSolveOptions options = unlimited_solve_options();
  options.seed_width = 2;
  options.seeded_inputs = seeded(pg, cg, kLosLoadRows);
  options.seeded_capture_inputs = seeded(pg, cg, kLosHeadRows);
  const GoldenRefSim golden;
  const BitParallelSim parallel;

  const auto los_pair = [&](uint32_t seed, int a) {
    std::map<std::string, bool> v1 = {{"A", a == 1}};
    for (const auto& [name, bits] : kLosLoadRows) v1[name] = seeded_value(bits, seed);
    std::map<std::string, bool> v2 = v1;
    v2["__ppi_ff1"] = v1.at("__ppi_ff0");
    for (const auto& [name, bits] : kLosHeadRows) v2[name] = seeded_value(bits, seed);
    return std::make_pair(v1, v2);
  };

  int sat = 0;
  int unsat = 0;
  for (const CompactFault& fault : enumerate_transition_faults(ng, cg)) {
    if (fault.exclusion != FaultExclusion::NONE) continue;
    bool detectable = false;
    for (uint32_t seed = 0; seed < 4 && !detectable; ++seed) {
      for (int a = 0; a < 2 && !detectable; ++a) {
        const auto [v1, v2] = los_pair(seed, a);
        detectable = golden.simulate_transition_fault(
            cg, vector_from_map(pg, v1), vector_from_map(pg, v2), fault);
      }
    }
    std::map<std::string, bool> v1;
    std::map<std::string, bool> v2;
    const SatSolveResult result = solve_scan_los_transition_fault(
        cg, pis, couples, heads, held, fault, options, v1, v2);
    REQUIRE((result == SatSolveResult::SAT) == detectable);
    if (result != SatSolveResult::SAT) {
      REQUIRE(result == SatSolveResult::UNSAT);
      ++unsat;
      continue;
    }
    ++sat;
    // One seed gives both the load and the launch-shift scan-in bits.
    std::map<std::string, bool> both;
    std::vector<std::pair<std::string, std::vector<int>>> rows;
    for (const auto& [name, bits] : kLosLoadRows) {
      both["v1:" + name] = v1.at(name);
      rows.emplace_back("v1:" + name, bits);
    }
    for (const auto& [name, bits] : kLosHeadRows) {
      both["v2:" + name] = v2.at(name);
      rows.emplace_back("v2:" + name, bits);
    }
    REQUIRE(loads_from_some_seed(rows, both, 2));
    const TestVector tv1 = vector_from_map(pg, v1);
    const TestVector tv2 = vector_from_map(pg, v2);
    REQUIRE(golden.simulate_transition_fault(cg, tv1, tv2, fault));
    REQUIRE(parallel.simulate_transition_single_fault(cg, tv1, tv2, fault));
  }
  REQUIRE(sat > 0);
  REQUIRE(unsat > 0);
}
