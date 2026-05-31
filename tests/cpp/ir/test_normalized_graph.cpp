#include <catch2/catch_test_macros.hpp>

#include "common/errors.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

using namespace faultflow;

TEST_CASE("normalize_cell_name OSU035", "[normalized_graph]") {
  REQUIRE(normalize_cell_name("OAI21X1") == "OAI21");
  REQUIRE(normalize_cell_name("AND2X2") == "AND2");
  REQUIRE(normalize_cell_name("NAND3X1") == "NAND3");
  REQUIRE(normalize_cell_name("INVX16") == "INV");
  REQUIRE(normalize_cell_name("CLKBUF1") == "BUF");
  REQUIRE(normalize_cell_name("BUFX4") == "BUF");
}

TEST_CASE("lookup_gate_type OSU035", "[normalized_graph]") {
  const CellMap yaml = CellMap::load(test::cell_map_path());
  REQUIRE(lookup_gate_type(yaml, "INVX1") == GateType::INV);
  REQUIRE(lookup_gate_type(yaml, "OAI21X1") == GateType::OAI21);
}

TEST_CASE("NormalizedGraph tiny_and2 levelization", "[normalized_graph]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  REQUIRE(ng.PIs.count(2) == 1);
  REQUIRE(ng.PIs.count(3) == 1);
  REQUIRE(ng.POs.count(4) == 1);
  int and_level = -1;
  for (const auto& [id, node] : ng.nodes) {
    if (node.gate_type == GateType::AND2) {
      and_level = node.level;
    }
  }
  REQUIRE(and_level >= 1);
}

TEST_CASE("Unsupported cell policy fail", "[normalized_graph]") {
  const CellMap yaml = CellMap::load(test::cell_map_path());
  const std::string json = R"({
    "modules": {
      "m": {
        "attributes": {"top": "1"},
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
