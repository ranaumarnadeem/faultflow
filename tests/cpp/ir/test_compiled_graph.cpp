#include <catch2/catch_test_macros.hpp>

#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("CompiledSimGraph c17", "[compiled_graph]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
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
  REQUIRE(gate_nodes >= 6);
  for (const auto& [yid, cidx] : cg.yosys_to_compiled) {
    REQUIRE(cg.compiled_to_yosys[cidx] == yid);
  }
}
