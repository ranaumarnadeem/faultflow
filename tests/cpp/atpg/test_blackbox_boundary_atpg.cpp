#include <catch2/catch_test_macros.hpp>

#include <map>
#include <string>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

// 9-A01: a fault that is observable ONLY at the blackbox-input TP (net `up`,
// which feeds the un-elaborated blackbox) must be SAT, and the emitted vector
// must verify through GoldenRefSim. This only holds if (a) the TP is in
// cg.observable and (b) the controllable pseudo-PI (bbout) is in the SAT PI
// set so good==faulty is enforced (otherwise the miter cheats and the vector
// fails re-simulation).
TEST_CASE("Phase9 SAT detects fault observable only at blackbox-input TP",
          "[blackbox9][atpg]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  const auto pis = atpg::ordered_pis(pg, cg);
  const int up = pg.net_id_by_name("up");

  CompactFault f;
  f.net_index = static_cast<uint32_t>(cg.yosys_to_compiled.at(up));
  f.type = FaultType::SA0;
  f.bit = 1;
  f.sa_mask = 1ULL;

  atpg::SatSolveOptions opts;
  std::map<std::string, bool> out;
  const atpg::SatSolveResult res =
      atpg::solve_stuck_at_fault(cg, pis, f, opts, out);
  REQUIRE(res == atpg::SatSolveResult::SAT);

  // Re-simulate the emitted vector through the golden oracle.
  TestVector v;
  for (const auto& pi : pis) {
    const auto it = out.find(pi.name);
    v.inputs[pi.yosys_id] = (it != out.end() && it->second);
  }
  GoldenRefSim sim;
  const auto ff = sim.simulate_fault_free(cg, v);
  const auto fa = sim.simulate_with_fault(cg, v, f);
  REQUIRE(sim.is_detected(cg, ff, fa));
}
