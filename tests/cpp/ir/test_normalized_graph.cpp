#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <vector>

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

TEST_CASE("CellMap delay metadata round-trips but is not authoritative",
          "[normalized_graph][timing]") {
  const CellMap map = CellMap::load(test::cell_map_path());

  // Cells that carry a `delay` field expose it (reporting-only metadata).
  const auto inv_delay = map.delay_for("sky130_fd_sc_hd__inv_1");
  REQUIRE(inv_delay.has_value());
  REQUIRE(inv_delay.value() == Catch::Approx(0.036));
  REQUIRE(map.delay_for("sky130_fd_sc_hd__nand2_1").has_value());

  // Cells without a delay field, and unknown cells, report nullopt.
  REQUIRE_FALSE(map.delay_for("sky130_fd_sc_hd__buf_1").has_value());
  REQUIRE_FALSE(map.delay_for("not_a_real_cell").has_value());

  // The delay field NEVER changes logic semantics: gate type is unaffected.
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__inv_1") == GateType::INV);
  REQUIRE(lookup_gate_type(map, "sky130_fd_sc_hd__nand2_1") == GateType::NAND2);
  const auto inv = map.lookup("sky130_fd_sc_hd__inv_1");
  REQUIRE(inv.has_value());
  REQUIRE(inv->gate_type == GateType::INV);
  REQUIRE(inv->inputs == std::vector<std::string>{"A"});
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

// ---------------------------------------------------------------------------
// Phase 9 — instance blackboxing + boundary observation
// Fixture tiny_blackbox_boundary.json:
//   a(2),b(3),c(4) PIs ; y(7) PO
//   g_up: and2_1 A=a,B=b -> up(5)      (upstream cone -> blackbox INPUT)
//   u_bb: inv_1  A=up    -> bbout(6)   (instance blackboxed by name)
//   g_dn: xor2_1 A=bbout,B=c -> y(7)   (downstream cone -> real PO)
// ---------------------------------------------------------------------------

TEST_CASE("Phase9 instance blackbox skips elaboration", "[normalized_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  // 9-N01: no node elaborated for the blackboxed instance.
  for (const auto& [id, node] : ng.nodes) {
    (void)id;
    REQUIRE(node.instance != "u_bb");
  }
  // 9-N04: bookkeeping + the surrounding gates still elaborated.
  REQUIRE(ng.blackbox_instances.count("u_bb") == 1);
  bool saw_up_gate = false;
  bool saw_dn_gate = false;
  for (const auto& [id, node] : ng.nodes) {
    (void)id;
    if (node.instance == "g_up") saw_up_gate = true;
    if (node.instance == "g_dn") saw_dn_gate = true;
  }
  REQUIRE(saw_up_gate);
  REQUIRE(saw_dn_gate);
}

TEST_CASE("Phase9 instance blackbox output becomes controllable pseudo-PI",
          "[normalized_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  const int bbout = pg.net_id_by_name("bbout");  // 6
  // 9-N02: output net driven by an INPUT source node; controllable, not excluded.
  REQUIRE(ng.pseudo_inputs.count(bbout) == 1);
  REQUIRE(ng.nets.at(bbout).is_pseudo_input);
  REQUIRE(ng.nets.at(bbout).driver >= 0);
  REQUIRE_FALSE(ng.nets.at(bbout).is_blackboxed);
}

TEST_CASE("Phase9 instance blackbox input becomes observable TP",
          "[normalized_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "fail", {"u_bb"});
  const int up = pg.net_id_by_name("up");  // 5
  // 9-N03
  REQUIRE(ng.TPs.count(up) == 1);
  REQUIRE(ng.nets.at(up).is_tp);
}

TEST_CASE("Phase9 unknown blackbox instance throws", "[normalized_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox_boundary.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  // 9-N05
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(pg, map, "fail", {"does_not_exist"}),
      ParseError);
}

TEST_CASE("Phase9 empty blackbox set leaves unsupported-cell path intact",
          "[normalized_graph][blackbox9]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  // 9-N06: existing unknown-type blackbox path unchanged; no pseudo-PI / TP.
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(pg, map, "blackbox", {});
  REQUIRE(ng.blackboxed.count(3) == 1);
  REQUIRE(ng.nets.at(3).is_blackboxed);
  REQUIRE(ng.pseudo_inputs.empty());
  REQUIRE(ng.TPs.empty());
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
