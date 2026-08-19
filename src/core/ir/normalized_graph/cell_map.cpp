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
      {"AND2", GateType::AND2},     {"AND2B", GateType::AND2B},
      {"AND3", GateType::AND3},
      {"AND4", GateType::AND4},     {"OR2", GateType::OR2},
      {"OR3", GateType::OR3},       {"OR4", GateType::OR4},
      {"NAND2", GateType::NAND2},   {"NAND3", GateType::NAND3},
      {"NAND4", GateType::NAND4},   {"NOR2", GateType::NOR2},
      {"NOR3", GateType::NOR3},     {"NOR4", GateType::NOR4},
      {"XOR2", GateType::XOR2},     {"XOR3", GateType::XOR3},
      {"XNOR2", GateType::XNOR2},   {"XNOR3", GateType::XNOR3},
      {"MUX2", GateType::MUX2},     {"MUX2_NI", GateType::MUX2_NI},
      {"AOI21", GateType::AOI21},   {"AOI22", GateType::AOI22},
      {"OAI21", GateType::OAI21},   {"OAI22", GateType::OAI22},
      {"A21O", GateType::A21O},     {"A21OI", GateType::A21OI},
      {"A21BO", GateType::A21BO},   {"A21BOI", GateType::A21BOI},
      {"A22O", GateType::A22O},     {"A22OI", GateType::A22OI},
      {"A211OI", GateType::A211OI}, {"A2111OI", GateType::A2111OI},
      {"A2111O", GateType::A2111O},
      {"A221O", GateType::A221O},   {"A221OI", GateType::A221OI},
      {"A31O", GateType::A31O},     {"A31OI", GateType::A31OI},
      {"A32O", GateType::A32O},     {"A32OI", GateType::A32OI},
      {"A41OI", GateType::A41OI},   {"O21A", GateType::O21A},
      {"O21AI", GateType::O21AI},   {"O21BAI", GateType::O21BAI},
      {"O22A", GateType::O22A},     {"O22AI", GateType::O22AI},
      {"O211AI", GateType::O211AI}, {"O221AI", GateType::O221AI},
      {"O31A", GateType::O31A},     {"O31AI", GateType::O31AI},
      {"O311A", GateType::O311A},   {"O311AI", GateType::O311AI},
      {"O32AI", GateType::O32AI},   {"O32A", GateType::O32A},
      {"NAND2B", GateType::NAND2B},
      {"NAND3B", GateType::NAND3B}, {"NAND4B", GateType::NAND4B},
      {"NAND4BB", GateType::NAND4BB},
      {"NOR2B", GateType::NOR2B},   {"NOR3B", GateType::NOR3B},
      {"NOR4B", GateType::NOR4B},   {"NOR4BB", GateType::NOR4BB},
      {"OR3B", GateType::OR3B},
      {"OR4B", GateType::OR4B},     {"ADDF", GateType::ADDF_S},
      {"ADDF_CO", GateType::ADDF_CO},
      {"ADDH", GateType::ADDH_S},   {"CONST0", GateType::CONST0},
      {"CONST1", GateType::CONST1}, {"WBR_IN", GateType::WBR_IN},
      {"WBR_OUT", GateType::WBR_OUT},
      // Sky130 compound gates
      {"A211O", GateType::A211O},     {"A222OI", GateType::A222OI},
      {"A2BB2OI", GateType::A2BB2OI}, {"A311O", GateType::A311O},
      {"A311OI", GateType::A311OI},   {"A41O", GateType::A41O},
      {"AND3B", GateType::AND3B},     {"AND4B", GateType::AND4B},
      {"MUX2I", GateType::MUX2I},     {"MUX4", GateType::MUX4},
      {"O211A", GateType::O211A},     {"O21BA", GateType::O21BA},
      {"O221A", GateType::O221A},     {"O2111A", GateType::O2111A},
      {"O2111AI", GateType::O2111AI}, {"O2BB2AI", GateType::O2BB2AI},
      {"O2BB2A", GateType::O2BB2A},  {"O41A", GateType::O41A},
      {"O41AI", GateType::O41AI},
      {"OR2B", GateType::OR2B},      {"OR4BB", GateType::OR4BB},
      {"AND4BB", GateType::AND4BB},  {"A2BB2O", GateType::A2BB2O},
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
  if (node.contains("enable")) {
    ff.enable = parse_ff_control(node.at("enable"));
  }
  if (node.contains("scan")) {
    const auto& scan = node.at("scan");
    ff.has_scan = true;
    if (scan.contains("in")) {
      ff.scan_in = scan.at("in").get<std::string>();
    } else if (scan.contains("input")) {
      ff.scan_in = scan.at("input").get<std::string>();
    } else {
      throw ParseError("FF scan metadata missing in/input pin");
    }
    if (scan.contains("enable")) {
      ff.scan_enable = scan.at("enable").get<std::string>();
    } else if (scan.contains("en")) {
      ff.scan_enable = scan.at("en").get<std::string>();
    } else {
      throw ParseError("FF scan metadata missing enable/en pin");
    }
    const std::string scan_level = scan.value(
        "enable_polarity", scan.value("polarity", std::string("HIGH")));
    ff.scan_enable_polarity = parse_polarity(scan_level);
  }
  ff.clear_preset_conflict_value =
      static_cast<uint8_t>(node.value("clear_preset_conflict_value", 0));
  if (ff.clear_preset_conflict_value > 1) {
    throw ParseError("FF conflict value must be 0 or 1");
  }
  return ff;
}

CellWBRMetadata parse_wbr_metadata(const nlohmann::json& node) {
  CellWBRMetadata wbr;
  wbr.present = true;
  const std::string side = node.at("side").get<std::string>();
  if (side == "input") {
    wbr.is_input = true;
  } else if (side == "output") {
    wbr.is_input = false;
  } else {
    throw ParseError("WBR side must be 'input' or 'output': " + side);
  }
  wbr.core_pin = node.at("core_pin").get<std::string>();
  wbr.sys_pin = node.at("sys_pin").get<std::string>();
  wbr.scan = node.value("scan", false);
  if (wbr.scan) {
    for (const char* pin : {"func_in_pin", "chain_in_pin", "chain_out_pin",
                            "scan_enable_pin", "clock_pin"}) {
      if (!node.contains(pin)) {
        throw ParseError(std::string("scan WBR metadata missing pin: ") + pin);
      }
    }
    wbr.func_in_pin = node.at("func_in_pin").get<std::string>();
    wbr.chain_in_pin = node.at("chain_in_pin").get<std::string>();
    wbr.chain_out_pin = node.at("chain_out_pin").get<std::string>();
    wbr.scan_enable_pin = node.at("scan_enable_pin").get<std::string>();
    wbr.clock_pin = node.at("clock_pin").get<std::string>();
  }
  return wbr;
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
  if (node.contains("wbr")) {
    entry.wbr = parse_wbr_metadata(node.at("wbr"));
  }
  if (node.contains("delay")) {
    entry.delay = node.at("delay").get<double>();
  }
  if (entry.node_type == NodeType::FF && !entry.ff.present) {
    throw ParseError("FF cell map entry missing ff metadata: " + pattern);
  }
  if ((entry.gate_type == GateType::WBR_IN ||
       entry.gate_type == GateType::WBR_OUT) &&
      !entry.wbr.present) {
    throw ParseError("WBR cell map entry missing wbr metadata: " + pattern);
  }
  // A scan WBR cell is BOTH an FF (capture/shift) and a wrapper (mode mux). Its
  // ff: and wbr: blocks must agree on the shared pins so downstream code can
  // trust either view (the lowering reads ff.* for the FF half and wbr.core_pin/
  // sys_pin for the mode-mux drive net).
  if (entry.wbr.present && entry.wbr.scan) {
    if (entry.node_type != NodeType::FF || !entry.ff.present) {
      throw ParseError("scan WBR cell must be node_type FF with ff metadata: " +
                       pattern);
    }
    if (entry.ff.data != entry.wbr.func_in_pin ||
        entry.ff.scan_in != entry.wbr.chain_in_pin ||
        entry.ff.output != entry.wbr.chain_out_pin ||
        entry.ff.scan_enable != entry.wbr.scan_enable_pin ||
        entry.ff.clock != entry.wbr.clock_pin) {
      throw ParseError("scan WBR ff/wbr pin cross-check mismatch: " + pattern);
    }
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
  const auto cached = lookup_cache_.find(raw_cell_type);
  if (cached != lookup_cache_.end()) {
    return cached->second;
  }
  std::optional<CellMapEntry> result;
  for (const auto& [pattern, entry] : patterns_) {
    if (pattern_match(pattern, raw_cell_type)) {
      result = entry;
      break;
    }
  }
  lookup_cache_.emplace(raw_cell_type, result);
  return result;
}

std::optional<double> CellMap::delay_for(
    const std::string& raw_cell_type) const {
  const auto entry = lookup(raw_cell_type);
  if (!entry) {
    return std::nullopt;
  }
  return entry->delay;
}

GateType lookup_gate_type(const CellMap& map, const std::string& raw) {
  const auto entry = map.lookup(raw);
  if (!entry) {
    throw UnsupportedCellError(raw);
  }
  return entry->gate_type;
}

}  // namespace faultflow
