#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "atpg/cone.hpp"
#include "common/errors.hpp"
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

TEST_CASE("nearest_reconvergent_stem finds the nearest fan-in stem",
          "[compiled_graph][cone]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const std::vector<int> driver = atpg::build_driver_index(cg);

  // Pick any gate and mark its direct input net as reconvergent; the nearest
  // reconvergent stem from the gate output must be that input (BFS depth 1).
  uint32_t gate_out = 0;
  uint32_t gate_in = 0;
  bool found = false;
  for (const auto& sn : cg.nodes) {
    if (sn.type != GateType::INPUT &&
        sn.in0 < static_cast<uint32_t>(cg.net_count)) {
      gate_out = sn.out;
      gate_in = sn.in0;
      found = true;
      break;
    }
  }
  REQUIRE(found);

  std::vector<char> reconvergent(static_cast<size_t>(cg.net_count), 0);
  reconvergent[gate_in] = 1;
  REQUIRE(atpg::nearest_reconvergent_stem(cg, gate_out, driver, reconvergent) ==
          static_cast<int>(gate_in));

  // No reconvergent nets in the cone -> -1.
  const std::vector<char> none(static_cast<size_t>(cg.net_count), 0);
  REQUIRE(atpg::nearest_reconvergent_stem(cg, gate_out, driver, none) == -1);
}

namespace {
// y = a & b, z = !a, both outputs; `attrs` is spliced into the top module's
// attribute object.
ParsedGraph two_output_graph(const std::string& attrs) {
  return ParsedGraph::from_json_string(R"({"modules": {"top2": {
    "attributes": {"top": "1")" + attrs + R"(},
    "ports": {
      "a": {"direction": "input",  "bits": [2]},
      "b": {"direction": "input",  "bits": [3]},
      "y": {"direction": "output", "bits": [5]},
      "z": {"direction": "output", "bits": [6]}
    },
    "cells": {
      "g_and": {"type": "sky130_fd_sc_hd__and2_1",
                "port_directions": {"A": "input", "B": "input", "X": "output"},
                "connections": {"A": [2], "B": [3], "X": [5]}},
      "g_inv": {"type": "sky130_fd_sc_hd__inv_1",
                "port_directions": {"A": "input", "Y": "output"},
                "connections": {"A": [2], "Y": [6]}}
    },
    "netnames": {"a": {"bits": [2]}, "b": {"bits": [3]}, "y": {"bits": [5]},
                 "z": {"bits": [6]}}
  }}})");
}
}  // namespace

TEST_CASE("An unobserved output stays a port but leaves the observable set",
          "[compiled_graph][unobserved]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng = NormalizedGraph::from_parsed(
      two_output_graph(R"(, "faultflow_unobserved_nets": "5")"), map);
  REQUIRE(ng.POs == std::set<int>{5, 6});
  REQUIRE(ng.unobserved == std::set<int>{5});
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
  REQUIRE(cg.observable ==
          std::vector<int>{cg.yosys_to_compiled.at(6)});

  // Without the attribute both outputs are observed.
  const CompiledSimGraph plain =
      GraphCompiler::compile(NormalizedGraph::from_parsed(two_output_graph(""), map));
  REQUIRE(plain.observable.size() == 2);
}

TEST_CASE("The unobserved-nets attribute names only output bits",
          "[compiled_graph][unobserved]") {
  const CellMap map = CellMap::load(test::cell_map_path());
  // net 2 is an input: masking it would mask nothing, silently.
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(
          two_output_graph(R"(, "faultflow_unobserved_nets": "5 2")"), map),
      ParseError);
  REQUIRE_THROWS_AS(
      NormalizedGraph::from_parsed(
          two_output_graph(R"(, "faultflow_unobserved_nets": "5x")"), map),
      ParseError);
}

TEST_CASE("combinational_reach stops at flops and names the pins it reaches",
          "[compiled_graph][cone]") {
  // a feeds u_ff.D directly and u_ff.CLK through g_clk (a gated clock); y
  // buffers the gated clock. se feeds only the flop's scan enable.
  const ParsedGraph pg = ParsedGraph::from_json_string(R"({"modules": {"top_ff": {
    "attributes": {"top": "1"},
    "ports": {
      "a":  {"direction": "input",  "bits": [2]},
      "b":  {"direction": "input",  "bits": [3]},
      "se": {"direction": "input",  "bits": [4]},
      "si": {"direction": "input",  "bits": [5]},
      "y":  {"direction": "output", "bits": [7]},
      "q":  {"direction": "output", "bits": [8]}
    },
    "cells": {
      "g_clk": {"type": "sky130_fd_sc_hd__and2_1",
                "port_directions": {"A": "input", "B": "input", "X": "output"},
                "connections": {"A": [2], "B": [3], "X": [6]}},
      "g_y": {"type": "sky130_fd_sc_hd__buf_1",
              "port_directions": {"A": "input", "X": "output"},
              "connections": {"A": [6], "X": [7]}},
      "u_ff": {"type": "sky130_fd_sc_hd__sdfxtp_1",
               "port_directions": {"CLK": "input", "D": "input", "SCD": "input",
                                   "SCE": "input", "Q": "output"},
               "connections": {"CLK": [6], "D": [2], "SCD": [5], "SCE": [4],
                               "Q": [8]}}
    },
    "netnames": {"a": {"bits": [2]}, "b": {"bits": [3]}, "se": {"bits": [4]},
                 "si": {"bits": [5]}, "gclk": {"bits": [6]}, "y": {"bits": [7]},
                 "q": {"bits": [8]}}
  }}})");
  const CellMap map = CellMap::load(test::cell_map_path());
  const CompiledSimGraph cg =
      GraphCompiler::compile(NormalizedGraph::from_parsed(pg, map));
  const auto cidx = [&](int yid) {
    return static_cast<uint32_t>(cg.yosys_to_compiled.at(yid));
  };
  int ff_node = -1;
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    if (cg.nodes[i].type == GateType::DFF) {
      ff_node = static_cast<int>(i);
    }
  }
  REQUIRE(ff_node >= 0);
  const auto ff = static_cast<uint32_t>(ff_node);

  const atpg::CombinationalReach from_a = atpg::combinational_reach(cg, {cidx(2)});
  const std::set<uint32_t> nets(from_a.nets.begin(), from_a.nets.end());
  REQUIRE(nets.count(cidx(7)) == 1);  // y, through the gated clock
  REQUIRE(nets.count(cidx(8)) == 0);  // q: the walk never passes the flop
  REQUIRE(from_a.flop_inputs ==
          std::vector<std::pair<uint32_t, int>>{{ff, 0}, {ff, 1}});

  const atpg::CombinationalReach from_se = atpg::combinational_reach(cg, {cidx(4)});
  REQUIRE(from_se.flop_inputs == std::vector<std::pair<uint32_t, int>>{{ff, 5}});
  REQUIRE(from_se.nets == std::vector<uint32_t>{cidx(4)});
}
