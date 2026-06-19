#pragma once

#include <map>
#include <set>
#include <string>
#include <vector>

#include "common/types.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

namespace faultflow {

struct NormNet {
  int id = 0;
  int canonical_id = 0;
  std::vector<int> original_ids;
  std::vector<std::string> names;
  int driver = -1;
  std::vector<int> fanout;
  bool is_pi = false;
  bool is_po = false;
  bool is_tp = false;
  bool is_clock = false;
  bool is_reset = false;
  bool is_blackboxed = false;
  // Phase 9: blackbox output net modeled as a controllable pseudo-PI (driven by
  // an INPUT source node). Distinct from a real PI — not a module port.
  bool is_pseudo_input = false;
};

struct NormNode {
  int id = 0;
  std::string instance;
  NodeType type = NodeType::GATE;
  GateType gate_type = GateType::BUF;
  std::map<std::string, int> input_pins;
  std::map<std::string, int> output_pins;
  int level = 0;
  FFConfig ff_config;
};

struct NormalizedGraph {
  std::map<int, NormNode> nodes;
  std::map<int, NormNet> nets;
  std::map<int, std::vector<int>> alias_map;
  std::map<int, std::vector<std::string>> name_map;
  std::set<int> PIs;
  std::set<int> POs;
  std::set<int> TPs;
  std::set<int> clocks;
  std::set<int> resets;
  std::set<int> blackboxed;
  // Phase 9: blackbox output nets exposed as controllable pseudo-PIs, and the
  // set of instance names blackboxed by name (boundary test interface).
  std::set<int> pseudo_inputs;
  std::set<std::string> blackbox_instances;

  static NormalizedGraph from_parsed(
      const ParsedGraph& parsed, const CellMap& cell_map,
      const std::string& unsupported_policy = "fail",
      const std::set<std::string>& blackbox_instances = {});
};

}  // namespace faultflow
