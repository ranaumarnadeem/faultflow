#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "atpg/cone.hpp"
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

TEST_CASE("CompiledSimGraph caches driver_index", "[compiled_graph][driver_index]") {
  for (const char* fixture : {"iscas85/synth_sky130/c17.json",
                              "iscas89/synth_sky130/s1238_bench.json"}) {
    const CompiledSimGraph cg = test::load_compiled_benchmark(fixture);
    // Sized to net_count and identical to a fresh structural rebuild.
    REQUIRE(cg.driver_index.size() == static_cast<size_t>(cg.net_count));
    REQUIRE(cg.driver_index == atpg::build_driver_index(cg));
    // Every entry points back to a node that actually drives that net.
    for (size_t net = 0; net < cg.driver_index.size(); ++net) {
      const int node = cg.driver_index[net];
      if (node >= 0) {
        REQUIRE(cg.nodes[static_cast<size_t>(node)].out == net);
      }
    }
  }
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

namespace {
std::vector<char> flag_set(int net_count, const std::vector<int>& nets) {
  std::vector<char> v(static_cast<size_t>(net_count), 0);
  for (int n : nets) {
    if (n >= 0 && n < net_count) v[static_cast<size_t>(n)] = 1;
  }
  return v;
}
}  // namespace

TEST_CASE("structural_reason: c17 nets are controllable + observable",
          "[compiled_graph][cone]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const std::vector<int> driver = atpg::build_driver_index(cg);
  const std::vector<char> observable = flag_set(cg.net_count, cg.observable);
  std::vector<int> controllable_nets = cg.pi_nets;
  controllable_nets.insert(controllable_nets.end(), cg.pseudo_pi_nets.begin(),
                           cg.pseudo_pi_nets.end());
  const std::vector<char> controllable = flag_set(cg.net_count, controllable_nets);

  for (int pi : cg.pi_nets) {
    const atpg::FaultStructuralReason r = atpg::structural_reason(
        cg, static_cast<uint32_t>(pi), driver, observable, controllable);
    // A PI is controllable (it is itself a controllable point).
    REQUIRE(r.reachable_from_pi);
    // reaches_observable must agree with the cone's reached_observables.
    const atpg::FaultCone cone = atpg::extract_fault_cone(
        cg, static_cast<uint32_t>(pi), driver, observable);
    REQUIRE(r.reaches_observable == !cone.reached_observables.empty());
  }
}

TEST_CASE("structural_reason: constant net is structurally uncontrollable",
          "[compiled_graph][cone]") {
  // A net driven only by a CONST cell has no PI in its backward cone, so the
  // solver can never justify a fault there -> structurally uncontrollable.
  const CompiledSimGraph cg = test::load_compiled("tiny_const.json");
  const std::vector<int> driver = atpg::build_driver_index(cg);
  const std::vector<char> observable = flag_set(cg.net_count, cg.observable);
  std::vector<int> controllable_nets = cg.pi_nets;
  controllable_nets.insert(controllable_nets.end(), cg.pseudo_pi_nets.begin(),
                           cg.pseudo_pi_nets.end());
  const std::vector<char> controllable = flag_set(cg.net_count, controllable_nets);

  const int c0 = cg.yosys_to_compiled.at(CONST0_NET_ID);
  const atpg::FaultStructuralReason r = atpg::structural_reason(
      cg, static_cast<uint32_t>(c0), driver, observable, controllable);
  REQUIRE_FALSE(r.reachable_from_pi);  // CONST net: no PI in its backward cone
}
