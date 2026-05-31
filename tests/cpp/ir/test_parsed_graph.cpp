#include <catch2/catch_test_macros.hpp>

#include "common/errors.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

using namespace faultflow;

TEST_CASE("ParsedGraph loads tiny_and2", "[parsed_graph]") {
  const ParsedGraph g = test::load_parsed("tiny_and2.json");
  REQUIRE(g.top == "tiny_and2");
  REQUIRE(g.modules.count("tiny_and2") == 1);
  REQUIRE(g.lib_cells.empty());

  const auto& mod = g.top_module();
  REQUIRE(mod.cells.at("u0").type == "AND2X1");
  REQUIRE(mod.ports.at("A").bits == std::vector<int>{2});
}

TEST_CASE("ParsedGraph parse errors", "[parsed_graph]") {
  REQUIRE_THROWS_AS(ParsedGraph::from_json_string("{}"), ParseError);
  REQUIRE_THROWS_AS(ParsedGraph::from_json_string("{bad json}"), ParseError);
}
