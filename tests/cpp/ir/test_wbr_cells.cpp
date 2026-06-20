#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

using namespace faultflow;

// Step 1 — IEEE 1500 wrapper boundary cells parse from the JSON cell maps with
// correct gate_type and wbr metadata (core/sys pin assignment, input vs output).

TEST_CASE("WBR input cell parses with wbr metadata (sky130)", "[wbr][cellmap]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  const auto e = map.lookup("$wbc_in_faultflow");
  REQUIRE(e.has_value());
  REQUIRE(e->node_type == NodeType::GATE);
  REQUIRE(e->gate_type == GateType::WBR_IN);
  REQUIRE(e->wbr.present);
  REQUIRE(e->wbr.is_input);
  REQUIRE(e->wbr.core_pin == "TO_CORE");
  REQUIRE(e->wbr.sys_pin == "FROM_SYS");
}

TEST_CASE("WBR output cell parses with wbr metadata (sky130)",
          "[wbr][cellmap]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  const auto e = map.lookup("$wbc_out_faultflow");
  REQUIRE(e.has_value());
  REQUIRE(e->node_type == NodeType::GATE);
  REQUIRE(e->gate_type == GateType::WBR_OUT);
  REQUIRE(e->wbr.present);
  REQUIRE_FALSE(e->wbr.is_input);
  REQUIRE(e->wbr.core_pin == "FROM_CORE");
  REQUIRE(e->wbr.sys_pin == "TO_SYS");
}

TEST_CASE("WBR cells parse in the OSU035 map too", "[wbr][cellmap]") {
  const CellMap map = CellMap::load(test::cell_map_path_osu());
  const auto in = map.lookup("$wbc_in_faultflow");
  const auto out = map.lookup("$wbc_out_faultflow");
  REQUIRE(in.has_value());
  REQUIRE(out.has_value());
  REQUIRE(in->gate_type == GateType::WBR_IN);
  REQUIRE(in->wbr.is_input);
  REQUIRE(out->gate_type == GateType::WBR_OUT);
  REQUIRE_FALSE(out->wbr.is_input);
}

TEST_CASE("WBR escaped cell-type variant also resolves", "[wbr][cellmap]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  REQUIRE(map.lookup("\\$wbc_in_faultflow").has_value());
  REQUIRE(map.lookup("\\$wbc_out_faultflow").has_value());
  REQUIRE(map.lookup("\\$wbc_in_faultflow")->wbr.present);
}

// Step 2 — from_parsed records wrapper_cells (YosysNetID space) and emits a
// WBR_IN/WBR_OUT node that drives the cell's stable output net.

namespace {

const NormWrapperCell* find_wc(const NormalizedGraph& ng, YosysNetID core_net) {
  for (const auto& wc : ng.wrapper_cells) {
    if (wc.core_net == core_net) {
      return &wc;
    }
  }
  return nullptr;
}

const NormNode* driver_node(const NormalizedGraph& ng, YosysNetID net) {
  auto it = ng.nets.find(net);
  if (it == ng.nets.end() || it->second.driver < 0) {
    return nullptr;
  }
  auto nit = ng.nodes.find(it->second.driver);
  return nit == ng.nodes.end() ? nullptr : &nit->second;
}

}  // namespace

TEST_CASE("WBR normalize: wrapper_cells recorded with core/sys nets",
          "[wbr][normalize]") {
  const ParsedGraph pg = test::load_parsed("tiny_wrapped_core.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map);

  const int g = pg.net_id_by_name("g");      // wi0 sys
  const int c = pg.net_id_by_name("c");      // wi1 sys
  const int ci0 = pg.net_id_by_name("ci0");  // wi0 core
  const int ci1 = pg.net_id_by_name("ci1");  // wi1 core
  const int co = pg.net_id_by_name("co");    // wo0 core
  const int y = pg.net_id_by_name("y");      // wo0 sys

  REQUIRE(ng.wrapper_cells.size() == 3);

  const NormWrapperCell* wi0 = find_wc(ng, ci0);
  REQUIRE(wi0 != nullptr);
  REQUIRE(wi0->is_input);
  REQUIRE(wi0->sys_net == g);

  const NormWrapperCell* wi1 = find_wc(ng, ci1);
  REQUIRE(wi1 != nullptr);
  REQUIRE(wi1->is_input);
  REQUIRE(wi1->sys_net == c);

  const NormWrapperCell* wo0 = find_wc(ng, co);
  REQUIRE(wo0 != nullptr);
  REQUIRE_FALSE(wo0->is_input);
  REQUIRE(wo0->sys_net == y);
}

TEST_CASE("WBR normalize: input cell drives core net, output cell drives sys net",
          "[wbr][normalize]") {
  const ParsedGraph pg = test::load_parsed("tiny_wrapped_core.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map);

  const int ci0 = pg.net_id_by_name("ci0");
  const int y = pg.net_id_by_name("y");

  // WBR_IN drives the core-side net (ci0).
  const NormNode* in_node = driver_node(ng, ci0);
  REQUIRE(in_node != nullptr);
  REQUIRE(in_node->gate_type == GateType::WBR_IN);

  // WBR_OUT drives the system-side net (y).
  const NormNode* out_node = driver_node(ng, y);
  REQUIRE(out_node != nullptr);
  REQUIRE(out_node->gate_type == GateType::WBR_OUT);
}

// Step 3 — build_mode_config reconfigures the control/observe point sets per
// the IEEE 1500 boundary truth table.

namespace {

bool has(const std::vector<uint32_t>& v, uint32_t x) {
  return std::find(v.begin(), v.end(), x) != v.end();
}

struct CompiledWrapped {
  ParsedGraph pg;
  CompiledSimGraph cg;
};

CompiledWrapped load_wrapped() {
  ParsedGraph pg = test::load_parsed("tiny_wrapped_core.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map);
  CompiledSimGraph cg = GraphCompiler::compile(ng);
  return {std::move(pg), std::move(cg)};
}

}  // namespace

TEST_CASE("WBR compiled side-table populated", "[wbr][mode]") {
  const CompiledWrapped w = load_wrapped();
  REQUIRE(w.cg.wrapper_cells.size() == 3);
  int inputs = 0;
  for (const auto& wc : w.cg.wrapper_cells) {
    inputs += wc.is_input ? 1 : 0;
  }
  REQUIRE(inputs == 2);
}

TEST_CASE("WBR mode FUNCTIONAL: base PI/observable, all wrapper actions PASS",
          "[wbr][mode]") {
  const CompiledWrapped w = load_wrapped();
  const ModeConfig mc = build_mode_config(w.cg, TestMode::FUNCTIONAL);
  REQUIRE(mc.stimulus_nets.size() == w.cg.pi_nets.size());
  for (int pi : w.cg.pi_nets) {
    REQUIRE(has(mc.stimulus_nets, static_cast<uint32_t>(pi)));
  }
  REQUIRE(mc.observable_nets.size() == w.cg.observable.size());
  for (int po : w.cg.observable) {
    REQUIRE(has(mc.observable_nets, static_cast<uint32_t>(po)));
  }
  REQUIRE(mc.safe_zero_nets.empty());
  for (uint8_t a : mc.wbr_action) {
    REQUIRE(a == static_cast<uint8_t>(WbrAction::PASS));
  }
}

TEST_CASE("WBR mode INTEST: control core inputs, observe core outputs",
          "[wbr][mode]") {
  const CompiledWrapped w = load_wrapped();
  auto C = [&](const char* n) {
    return static_cast<uint32_t>(
        w.cg.yosys_to_compiled.at(w.pg.net_id_by_name(n)));
  };
  const ModeConfig mc = build_mode_config(w.cg, TestMode::INTEST);

  // control = core input nets {ci0, ci1}
  REQUIRE(mc.stimulus_nets.size() == 2);
  REQUIRE(has(mc.stimulus_nets, C("ci0")));
  REQUIRE(has(mc.stimulus_nets, C("ci1")));
  // observe = core output net {co}
  REQUIRE(mc.observable_nets.size() == 1);
  REQUIRE(has(mc.observable_nets, C("co")));
  // sys output net y forced safe-0
  REQUIRE(has(mc.safe_zero_nets, C("y")));
  // wrapper actions
  REQUIRE(mc.wbr_action[C("ci0")] ==
          static_cast<uint8_t>(WbrAction::SKIP_STIMULUS));
  REQUIRE(mc.wbr_action[C("ci1")] ==
          static_cast<uint8_t>(WbrAction::SKIP_STIMULUS));
  REQUIRE(mc.wbr_action[C("y")] ==
          static_cast<uint8_t>(WbrAction::FORCE_ZERO));
}

TEST_CASE("WBR mode EXTEST: roles flip to drive/observe the system side",
          "[wbr][mode]") {
  const CompiledWrapped w = load_wrapped();
  auto C = [&](const char* n) {
    return static_cast<uint32_t>(
        w.cg.yosys_to_compiled.at(w.pg.net_id_by_name(n)));
  };
  const ModeConfig mc = build_mode_config(w.cg, TestMode::EXTEST);

  // control = output-wrapper sys net {y} + top PIs {a,b,c}
  REQUIRE(has(mc.stimulus_nets, C("y")));
  REQUIRE(has(mc.stimulus_nets, C("a")));
  REQUIRE(has(mc.stimulus_nets, C("b")));
  REQUIRE(has(mc.stimulus_nets, C("c")));
  // observe = input-wrapper sys net {g} (interconnect endpoint)
  REQUIRE(has(mc.observable_nets, C("g")));
  // y is a control point now, NOT observed
  REQUIRE_FALSE(has(mc.observable_nets, C("y")));
  // core input nets forced safe-0
  REQUIRE(has(mc.safe_zero_nets, C("ci0")));
  REQUIRE(has(mc.safe_zero_nets, C("ci1")));
  // wrapper actions flipped vs INTEST
  REQUIRE(mc.wbr_action[C("ci0")] ==
          static_cast<uint8_t>(WbrAction::FORCE_ZERO));
  REQUIRE(mc.wbr_action[C("y")] ==
          static_cast<uint8_t>(WbrAction::SKIP_STIMULUS));
}
