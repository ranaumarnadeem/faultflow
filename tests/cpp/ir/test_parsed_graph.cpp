#include <catch2/catch_test_macros.hpp>

#include "common/errors.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

using namespace faultflow;

TEST_CASE("ParsedGraph loads synthesized c17", "[parsed_graph]") {
  const ParsedGraph g =
      test::load_parsed_benchmark("iscas85/synth/c17.json");
  REQUIRE(g.top == "c17");
  REQUIRE(g.modules.count("c17") == 1);
  REQUIRE(g.lib_cells.count("NAND2X1") == 1);
  REQUIRE(g.lib_cells.count("INVX1") == 1);
  REQUIRE_FALSE(g.lib_cells.count("c17"));

  const auto& mod = g.top_module();
  REQUIRE(mod.ports.at("N1").bits == std::vector<int>{2});
  REQUIRE(mod.ports.at("N22").bits == std::vector<int>{7});
  REQUIRE(mod.cells.size() == 6);
}

TEST_CASE("ParsedGraph net_id_by_name c17", "[parsed_graph]") {
  const ParsedGraph g =
      test::load_parsed_benchmark("iscas85/synth/c17.json");
  REQUIRE(g.net_id_by_name("N1") == 2);
  REQUIRE(g.net_id_by_name("N23") == 8);
}

TEST_CASE("ParsedGraph parse errors", "[parsed_graph]") {
  REQUIRE_THROWS_AS(ParsedGraph::from_json_string("{}"), ParseError);
  REQUIRE_THROWS_AS(ParsedGraph::from_json_string("{bad json}"), ParseError);
}
