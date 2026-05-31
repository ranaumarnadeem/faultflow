#include <catch2/catch_test_macros.hpp>

#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("CompiledSimGraph tiny_inv", "[compiled_graph]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  REQUIRE(cg.net_count >= 2);
  REQUIRE(cg.observable.size() >= 1);
  REQUIRE(cg.pi_nets.size() == 1);
  for (const auto& sn : cg.nodes) {
    REQUIRE(sn.out < static_cast<uint32_t>(cg.net_count));
  }
  for (const auto& [yid, cidx] : cg.yosys_to_compiled) {
    REQUIRE(cg.compiled_to_yosys[cidx] == yid);
  }
}
