#include <catch2/catch_test_macros.hpp>

#include "common/errors.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

using namespace faultflow;

TEST_CASE("CellMap pattern-matches sky130 drive-strength variants", "[normalized_graph]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__inv_1") == GateType::INV);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__and2_4") == GateType::AND2);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__nand3_2") == GateType::NAND3);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__clkbuf_8") == GateType::BUF);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__buf_4") == GateType::BUF);
}

TEST_CASE("lookup_gate_type Sky130", "[normalized_graph]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__inv_1") == GateType::INV);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__clkbuf_1") == GateType::BUF);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__o21ai_1") == GateType::O21AI);
}

TEST_CASE("NormalizedGraph c17 levelization", "[normalized_graph]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  REQUIRE(ng.PIs.size() == 5);
  REQUIRE(ng.POs.size() == 2);
  int max_level = -1;
  for (const auto& [id, node] : ng.nodes) {
    (void)id;
    if (node.gate_type != GateType::INPUT) {
      max_level = std::max(max_level, node.level);
    }
  }
  REQUIRE(max_level >= 1);
}

TEST_CASE("Unsupported cell policy fail", "[normalized_graph]") {
  const CellMap yaml = CellMap::load(test::cell_map_path());
  const std::string json = R"({
    "modules": {
      "m": {
        "attributes": {"top": "00000000000000000000000000000001"},
        "ports": {"A": {"direction": "input", "bits": [2]}, "Y": {"direction": "output", "bits": [3]}},
        "cells": {"u0": {"type": "TBUFX1", "connections": {"A": [2], "EN": [3], "Y": [4]}}},
        "netnames": {}
      }
    }
  })";
  const ParsedGraph pg = ParsedGraph::from_json_string(json);
  REQUIRE_THROWS_AS(NormalizedGraph::from_parsed(pg, yaml, "fail"),
                    UnsupportedCellError);
}

TEST_CASE("NormalizedGraph rejects unknown cells by default", "[normalized_graph]") {
  const ParsedGraph pg = test::load_parsed("tiny_unknown.json");
  const CellMap yaml = CellMap::load(test::cell_map_path());
  REQUIRE_THROWS_AS(NormalizedGraph::from_parsed(pg, yaml, "fail"),
                    UnsupportedCellError);
}

TEST_CASE("NormalizedGraph blackbox policy tags unknown outputs", "[normalized_graph]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox.json");
  const CellMap yaml = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng = NormalizedGraph::from_parsed(pg, yaml, "blackbox");
  REQUIRE(ng.blackboxed.count(3) == 1);
  REQUIRE(ng.nets.at(3).is_blackboxed);
}

TEST_CASE("Deferred Sky130 latch and tbuf cells hard-fail while unsupported",
          "[normalized_graph]") {
  const CellMap yaml = CellMap::load(test::cell_map_path());
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(test::load_parsed("tiny_latch.json"), yaml),
      UnsupportedCellError);
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(test::load_parsed("tiny_tbuf.json"), yaml),
      UnsupportedCellError);
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(test::load_parsed("tiny_tbuf.json"), yaml,
                                   "blackbox"),
      UnsupportedCellError);
}

TEST_CASE("NormalizedGraph keeps bus bits distinct", "[normalized_graph]") {
  const NormalizedGraph ng = test::load_normalized("tiny_bus_alias.json");
  REQUIRE(ng.PIs.count(2) == 1);
  REQUIRE(ng.PIs.count(3) == 1);
  REQUIRE(ng.POs.count(4) == 1);
  REQUIRE(ng.POs.count(5) == 1);
  REQUIRE(ng.nets.at(2).canonical_id == 2);
  REQUIRE(ng.nets.at(3).canonical_id == 3);
  REQUIRE(ng.alias_map.at(2) == std::vector<int>{2});
  REQUIRE(ng.alias_map.at(3) == std::vector<int>{3});
}

TEST_CASE("NormalizedGraph identifies clock and reset names", "[normalized_graph]") {
  const NormalizedGraph ng = test::load_normalized("tiny_clock_reset.json");
  REQUIRE(ng.clocks.count(2) == 1);
  REQUIRE(ng.resets.count(3) == 1);
  REQUIRE(ng.nets.at(2).is_clock);
  REQUIRE(ng.nets.at(3).is_reset);
}
