#include <catch2/catch_test_macros.hpp>

#include <vector>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

// Step 5 — IEEE 1500 wrapper boundary simulation success gate:
//   * FUNCTIONAL wrappers are invisible (bit-identical to the no-wrapper core).
//   * INTEST and EXTEST: GoldenRefSim agrees with BitParallelSim on every
//     observable, fault-free and for every enumerated fault.
//   * The same wrapper cells flip control/observe role between modes.

using namespace faultflow;

namespace {

struct Wrapped {
  ParsedGraph pg;
  NormalizedGraph ng;
  CompiledSimGraph cg;
};

Wrapped load(const char* fixture) {
  ParsedGraph pg = test::load_parsed(fixture);
  const CellMap map = CellMap::load(test::cell_map_path());
  NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map);
  CompiledSimGraph cg = GraphCompiler::compile(ng);
  return {std::move(pg), std::move(ng), std::move(cg)};
}

// Complete input space over a set of Yosys net ids (the mode's control points).
test::VectorSet vectors_over(const std::vector<int>& yids) {
  return test::generate_complete_input_space(yids);
}

std::vector<int> stimulus_yids(const CompiledSimGraph& cg, const ModeConfig& mc) {
  std::vector<int> yids;
  for (uint32_t s : mc.stimulus_nets) {
    yids.push_back(cg.compiled_to_yosys[s]);
  }
  return yids;
}

// Count golden-vs-bit-parallel detection disagreements for one mode.
int mode_mismatches(const CompiledSimGraph& cg, const NormalizedGraph& ng,
                    const ModeConfig& mc) {
  GoldenRefSim golden;
  BitParallelSim parallel;
  const auto faults = enumerate_faults(ng, cg);
  const auto vs = vectors_over(stimulus_yids(cg, mc));
  int mismatches = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    for (const auto& vec : vs.vectors) {
      const auto ff = golden.simulate_fault_free(cg, vec, mc);
      const auto fa = golden.simulate_with_fault(cg, vec, fault, mc);
      const bool g_detect = golden.is_detected(cg, ff, fa, mc);
      const bool p_detect = parallel.simulate_single_fault(cg, vec, fault, mc);
      if (g_detect != p_detect) {
        ++mismatches;
      }
    }
  }
  return mismatches;
}

}  // namespace

TEST_CASE("WBR FUNCTIONAL is invisible: identical to the no-wrapper core",
          "[wbr][modes][golden_ref]") {
  const Wrapped w = load("tiny_wrapped_core.json");
  const Wrapped n = load("tiny_wrapped_core_nowrap.json");
  GoldenRefSim golden;

  const ModeConfig mc = build_mode_config(w.cg, TestMode::FUNCTIONAL);
  const int a = w.pg.net_id_by_name("a");
  const int b = w.pg.net_id_by_name("b");
  const int c = w.pg.net_id_by_name("c");
  const int yw = w.pg.net_id_by_name("y");
  const int yn = n.pg.net_id_by_name("y");

  for (int mask = 0; mask < 8; ++mask) {
    TestVector vec;
    vec.inputs[a] = (mask & 1) != 0;
    vec.inputs[b] = (mask & 2) != 0;
    vec.inputs[c] = (mask & 4) != 0;
    // Wrapped core in FUNCTIONAL mode vs the plain (no-wrapper) core.
    const auto rw = golden.simulate_fault_free(w.cg, vec, mc);
    const auto rn = golden.simulate_fault_free(n.cg, vec);
    REQUIRE(rw.at(yw) == rn.at(yn));
  }
}

TEST_CASE("WBR INTEST: golden == bit-parallel (0 mismatches)",
          "[wbr][modes][golden_ref]") {
  const Wrapped w = load("tiny_wrapped_core.json");
  const ModeConfig mc = build_mode_config(w.cg, TestMode::INTEST);
  REQUIRE(mode_mismatches(w.cg, w.ng, mc) == 0);
}

TEST_CASE("WBR EXTEST: golden == bit-parallel (0 mismatches)",
          "[wbr][modes][golden_ref]") {
  const Wrapped w = load("tiny_wrapped_core.json");
  const ModeConfig mc = build_mode_config(w.cg, TestMode::EXTEST);
  REQUIRE(mode_mismatches(w.cg, w.ng, mc) == 0);
}

TEST_CASE("WBR INTEST: core inputs controllable, core outputs observable",
          "[wbr][modes][golden_ref]") {
  const Wrapped w = load("tiny_wrapped_core.json");
  GoldenRefSim golden;
  const ModeConfig mc = build_mode_config(w.cg, TestMode::INTEST);

  const int ci0 = w.pg.net_id_by_name("ci0");
  const int ci1 = w.pg.net_id_by_name("ci1");
  const int co = w.pg.net_id_by_name("co");
  const int y = w.pg.net_id_by_name("y");
  const int a = w.pg.net_id_by_name("a");
  const int b = w.pg.net_id_by_name("b");

  // co = ci0 ^ ci1, controlled directly through the input wrapper cells.
  TestVector v10;
  v10.inputs[ci0] = true;
  v10.inputs[ci1] = false;
  // Drive top PIs too — they must NOT influence the core (decoupled in INTEST).
  v10.inputs[a] = true;
  v10.inputs[b] = true;
  const auto r10 = golden.simulate_fault_free(w.cg, v10, mc);
  REQUIRE(r10.at(co) == true);
  REQUIRE(r10.at(y) == false);  // sys side forced safe-0

  TestVector v11;
  v11.inputs[ci0] = true;
  v11.inputs[ci1] = true;
  const auto r11 = golden.simulate_fault_free(w.cg, v11, mc);
  REQUIRE(r11.at(co) == false);  // observable core output toggled by control
}

TEST_CASE("WBR EXTEST: same cells flip to drive/observe the interconnect",
          "[wbr][modes][golden_ref]") {
  const Wrapped w = load("tiny_wrapped_core.json");
  GoldenRefSim golden;
  const ModeConfig mc = build_mode_config(w.cg, TestMode::EXTEST);

  const int a = w.pg.net_id_by_name("a");
  const int b = w.pg.net_id_by_name("b");
  const int g = w.pg.net_id_by_name("g");
  const int ci0 = w.pg.net_id_by_name("ci0");

  // g = a & b is the interconnect endpoint observed at the input wrapper cell.
  TestVector v11;
  v11.inputs[a] = true;
  v11.inputs[b] = true;
  const auto r11 = golden.simulate_fault_free(w.cg, v11, mc);
  REQUIRE(r11.at(g) == true);
  REQUIRE(r11.at(ci0) == false);  // core side forced safe-0

  TestVector v01;
  v01.inputs[a] = false;
  v01.inputs[b] = true;
  const auto r01 = golden.simulate_fault_free(w.cg, v01, mc);
  REQUIRE(r01.at(g) == false);  // observable interconnect toggled by control
}
