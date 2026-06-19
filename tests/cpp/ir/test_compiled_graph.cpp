#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "helpers/test_helpers.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

using namespace faultflow;

TEST_CASE("CompiledSimGraph c17", "[compiled_graph]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  REQUIRE(cg.net_count >= 8);
  REQUIRE(cg.observable.size() == 2);
  REQUIRE(cg.pi_nets.size() == 5);
  int gate_nodes = 0;
  for (const auto& sn : cg.nodes) {
    REQUIRE(sn.out < static_cast<uint32_t>(cg.net_count));
    if (sn.type != GateType::INPUT && sn.type != GateType::BUF) {
      ++gate_nodes;
    }
  }
  REQUIRE(gate_nodes >= 3);
  for (const auto& [yid, cidx] : cg.yosys_to_compiled) {
    REQUIRE(cg.compiled_to_yosys[cidx] == yid);
  }
}

TEST_CASE("Phase9 blackbox boundary observable + pi_nets",
          "[compiled_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
  const int up_c = cg.yosys_to_compiled.at(pg.net_id_by_name("up"));      // TP
  const int y_c = cg.yosys_to_compiled.at(pg.net_id_by_name("y"));        // PO
  const int bbout_c = cg.yosys_to_compiled.at(pg.net_id_by_name("bbout"));// pseudo-PI
  // 9-C01: observable = PO + TP (blackbox input net).
  REQUIRE(std::count(cg.observable.begin(), cg.observable.end(), up_c) == 1);
  REQUIRE(std::count(cg.observable.begin(), cg.observable.end(), y_c) == 1);
  // 9-C02: pi_nets includes the controllable pseudo-PI (blackbox output net).
  REQUIRE(std::count(cg.pi_nets.begin(), cg.pi_nets.end(), bbout_c) == 1);
}

TEST_CASE("CompiledSimGraph constant drivers", "[compiled_graph]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_const.json");
  bool saw_const0 = false;
  bool saw_const1 = false;
  for (const auto& sn : cg.nodes) {
    REQUIRE(sn.out < static_cast<uint32_t>(cg.net_count));
    if (sn.type == GateType::CONST0) {
      saw_const0 = true;
    }
    if (sn.type == GateType::CONST1) {
      saw_const1 = true;
    }
  }
  REQUIRE(saw_const0);
  REQUIRE(saw_const1);
  REQUIRE(cg.yosys_to_compiled.count(CONST0_NET_ID) == 1);
  REQUIRE(cg.yosys_to_compiled.count(CONST1_NET_ID) == 1);
}
