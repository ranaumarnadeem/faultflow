#include "ir/normalized_graph/cell_map.hpp"

#include <fstream>
#include <map>
#include <regex>
#include <stdexcept>

#include <nlohmann/json.hpp>

#include "common/errors.hpp"

namespace faultflow {
namespace {

bool pattern_match(const std::string& pattern, const std::string& name) {
  if (!pattern.empty() && pattern.back() == '*') {
    const std::string prefix = pattern.substr(0, pattern.size() - 1);
    return name.size() >= prefix.size() &&
           name.compare(0, prefix.size(), prefix) == 0;
  }
  return pattern == name;
}

GateType parse_gate_type(const std::string& s) {
  static const std::map<std::string, GateType> kMap = {
      {"INV", GateType::INV},       {"BUF", GateType::BUF},
      {"AND2", GateType::AND2},     {"OR2", GateType::OR2},
      {"NAND2", GateType::NAND2},   {"NAND3", GateType::NAND3},
      {"NOR2", GateType::NOR2},     {"NOR3", GateType::NOR3},
      {"XOR2", GateType::XOR2},     {"XNOR2", GateType::XNOR2},
      {"MUX2", GateType::MUX2},     {"AOI21", GateType::AOI21},
      {"AOI22", GateType::AOI22},   {"OAI21", GateType::OAI21},
      {"OAI22", GateType::OAI22},   {"ADDF", GateType::ADDF_S},
      {"ADDH", GateType::ADDH_S},   {"CONST0", GateType::CONST0},
      {"CONST1", GateType::CONST1},
  };
  auto it = kMap.find(s);
  if (it == kMap.end()) {
    throw ParseError("Unknown gate_type in cell map: " + s);
  }
  return it->second;
}

NodeType parse_node_type(const std::string& s) {
  if (s == "GATE") return NodeType::GATE;
  if (s == "FF") return NodeType::FF;
  if (s == "LATCH") return NodeType::LATCH;
  if (s == "TBUF") return NodeType::TBUF;
  if (s == "ICG") return NodeType::ICG;
  if (s == "CONST") return NodeType::CONST;
  throw ParseError("Unknown node_type: " + s);
}

TriggerType parse_trigger(const std::string& s) {
  if (s == "POSEDGE") return TriggerType::POSEDGE;
  if (s == "NEGEDGE") return TriggerType::NEGEDGE;
  throw ParseError("Unknown FF trigger: " + s);
}

Polarity parse_polarity(const std::string& s) {
  if (s == "HIGH" || s == "ACTIVE_HIGH") return Polarity::ACTIVE_HIGH;
  if (s == "LOW" || s == "ACTIVE_LOW") return Polarity::ACTIVE_LOW;
  throw ParseError("Unknown FF polarity/level: " + s);
}

CellFFControl parse_ff_control(const nlohmann::json& node) {
  CellFFControl ctrl;
  ctrl.present = true;
  ctrl.pin = node.at("pin").get<std::string>();
  const std::string level =
      node.value("level", node.value("polarity", std::string("HIGH")));
  ctrl.polarity = parse_polarity(level);
  ctrl.value = static_cast<uint8_t>(node.value("value", 0));
  if (ctrl.value > 1) {
    throw ParseError("FF control value must be 0 or 1");
  }
  return ctrl;
}

CellFFMetadata parse_ff_metadata(const nlohmann::json& node) {
  CellFFMetadata ff;
  ff.present = true;
  ff.clock = node.at("clock").get<std::string>();
  ff.data = node.at("data").get<std::string>();
  ff.output = node.value("output", std::string("Q"));
  ff.trigger = parse_trigger(node.at("trigger").get<std::string>());
  if (node.contains("clear")) {
    ff.clear = parse_ff_control(node.at("clear"));
  }
  if (node.contains("preset")) {
    ff.preset = parse_ff_control(node.at("preset"));
  }
  ff.clear_preset_conflict_value =
      static_cast<uint8_t>(node.value("clear_preset_conflict_value", 0));
  if (ff.clear_preset_conflict_value > 1) {
    throw ParseError("FF conflict value must be 0 or 1");
  }
  return ff;
}

CellMapEntry parse_entry(const std::string& pattern, const nlohmann::json& node) {
  CellMapEntry entry;
  if (node.value("unsupported", false)) {
    entry.unsupported = true;
    entry.node_type = NodeType::GATE;
    entry.gate_type = GateType::BUF;
    return entry;
  }
  entry.node_type = parse_node_type(node.at("node_type").get<std::string>());
  if (node.contains("gate_type")) {
    entry.gate_type = parse_gate_type(node.at("gate_type").get<std::string>());
  }
  if (node.contains("inputs")) {
    for (const auto& pin : node.at("inputs")) {
      entry.inputs.push_back(pin.get<std::string>());
    }
  }
  if (node.contains("outputs")) {
    for (auto it = node.at("outputs").begin(); it != node.at("outputs").end();
         ++it) {
      entry.outputs[it.key()] = it.value().get<std::string>();
    }
  }
  if (node.contains("ff")) {
    entry.ff = parse_ff_metadata(node.at("ff"));
  }
  if (entry.node_type == NodeType::FF && !entry.ff.present) {
    throw ParseError("FF cell map entry missing ff metadata: " + pattern);
  }
  return entry;
}

}  // namespace

std::string normalize_cell_name(const std::string& raw) {
  static const std::map<std::string, std::string> kAlias = {
      {"TIEHI", "CONST1"},
      {"TIELO", "CONST0"},
  };
  if (kAlias.count(raw)) {
    return kAlias.at(raw);
  }
  if (raw.rfind("CLKBUF", 0) == 0) {
    return "BUF";
  }
  static const std::regex kSuffix(R"(X\d+$)");
  return std::regex_replace(raw, kSuffix, "");
}

CellMap CellMap::load(const std::string& path) {
  CellMap map;
  std::ifstream in(path);
  if (!in) {
    throw ParseError("Cannot open cell map: " + path);
  }
  nlohmann::json root;
  in >> root;
  if (!root.is_object()) {
    throw ParseError("Cell map root must be a JSON object");
  }
  for (auto it = root.begin(); it != root.end(); ++it) {
    const std::string pattern = it.key();
    map.patterns_.emplace_back(pattern, parse_entry(pattern, it.value()));
  }
  return map;
}

std::optional<CellMapEntry> CellMap::lookup(
    const std::string& raw_cell_type) const {
  for (const auto& [pattern, entry] : patterns_) {
    if (pattern_match(pattern, raw_cell_type)) {
      return entry;
    }
  }
  return std::nullopt;
}

GateType lookup_gate_type(const CellMap& map, const std::string& raw) {
  const auto entry = map.lookup(raw);
  if (!entry) {
    throw UnsupportedCellError(raw);
  }
  return entry->gate_type;
}

}  // namespace faultflow
