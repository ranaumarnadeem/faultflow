#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

// Build the Phase-9 boundary fixture with u_bb blackboxed.
//   a(2),b(3),c(4) PIs ; y(7) PO
//   g_up: and2_1 -> up(5)   ; u_bb: inv_1 -> bbout(6) ; g_dn: xor2_1 -> y(7)
struct BbGraph {
  ParsedGraph pg;
  NormalizedGraph ng;
  CompiledSimGraph cg;
};

BbGraph build_blackboxed() {
  ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  CompiledSimGraph cg = GraphCompiler::compile(ng);
  return {std::move(pg), std::move(ng), std::move(cg)};
}

}  // namespace

TEST_CASE("Phase9 blackbox boundary: golden == bit-parallel (0 mismatches)",
          "[blackbox9][golden_ref]") {
  const BbGraph g = build_blackboxed();
  const auto faults = enumerate_faults(g.ng, g.cg);
  // Full input space over real PIs (a,b,c) and the controllable pseudo-PI bbout.
  const auto vs = test::generate_complete_input_space(
      {g.pg.net_id_by_name("a"), g.pg.net_id_by_name("b"),
       g.pg.net_id_by_name("c"), g.pg.net_id_by_name("bbout")});
  const auto mm = test::verify_parallel_matches_golden(g.cg, g.ng, faults, vs);
  REQUIRE(mm.empty());  // 9-S01
}

TEST_CASE("Phase9 blackbox output is controllable (not constant-0)",
          "[blackbox9][golden_ref]") {
  const BbGraph g = build_blackboxed();
  GoldenRefSim sim;
  const int a = g.pg.net_id_by_name("a");
  const int b = g.pg.net_id_by_name("b");
  const int c = g.pg.net_id_by_name("c");
  const int bbout = g.pg.net_id_by_name("bbout");
  const int y = g.pg.net_id_by_name("y");

  TestVector v0;
  v0.inputs[a] = true;
  v0.inputs[b] = true;
  v0.inputs[c] = false;
  v0.inputs[bbout] = false;
  TestVector v1 = v0;
  v1.inputs[bbout] = true;

  // y = bbout XOR c ; with c=0, toggling the pseudo-PI must flip y.
  const auto r0 = sim.simulate_fault_free(g.cg, v0);
  const auto r1 = sim.simulate_fault_free(g.cg, v1);
  REQUIRE(r0.at(y) != r1.at(y));  // 9-S02
}

TEST_CASE("Phase9 blackbox input fault observable only at the TP",
          "[blackbox9][golden_ref]") {
  const BbGraph g = build_blackboxed();
  GoldenRefSim sim;
  const int a = g.pg.net_id_by_name("a");
  const int b = g.pg.net_id_by_name("b");
  const int c = g.pg.net_id_by_name("c");
  const int bbout = g.pg.net_id_by_name("bbout");
  const int up = g.pg.net_id_by_name("up");  // blackbox input net (TP only)

  // up = a & b. SA0 needs good up=1 (a=b=1); SA1 needs good up=0 (a=0).
  CompactFault sa0;
  sa0.net_index = static_cast<uint32_t>(g.cg.yosys_to_compiled.at(up));
  sa0.type = FaultType::SA0;
  sa0.bit = 1;
  sa0.sa_mask = 1ULL;
  TestVector v_hi;
  v_hi.inputs[a] = true;
  v_hi.inputs[b] = true;
  v_hi.inputs[c] = false;
  v_hi.inputs[bbout] = false;
  const auto ff_hi = sim.simulate_fault_free(g.cg, v_hi);
  const auto fa0 = sim.simulate_with_fault(g.cg, v_hi, sa0);
  REQUIRE(sim.is_detected(g.cg, ff_hi, fa0));  // 9-S03 (detected at TP)

  CompactFault sa1;
  sa1.net_index = static_cast<uint32_t>(g.cg.yosys_to_compiled.at(up));
  sa1.type = FaultType::SA1;
  sa1.bit = 1;
  sa1.sa_mask = 1ULL;
  TestVector v_lo;
  v_lo.inputs[a] = false;
  v_lo.inputs[b] = true;
  v_lo.inputs[c] = false;
  v_lo.inputs[bbout] = false;
  const auto ff_lo = sim.simulate_fault_free(g.cg, v_lo);
  const auto fa1 = sim.simulate_with_fault(g.cg, v_lo, sa1);
  REQUIRE(sim.is_detected(g.cg, ff_lo, fa1));
}
