#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

struct CellFFControl {
  std::string pin;
  Polarity polarity = Polarity::ACTIVE_HIGH;
  uint8_t value = 0;
  bool present = false;
};

struct CellFFMetadata {
  std::string clock;
  std::string data;
  std::string output = "Q";
  TriggerType trigger = TriggerType::POSEDGE;
  CellFFControl clear;
  CellFFControl preset;
  uint8_t clear_preset_conflict_value = 0;
  bool present = false;
};

struct CellMapEntry {
  NodeType node_type = NodeType::GATE;
  GateType gate_type = GateType::BUF;
  std::vector<std::string> inputs;
  std::map<std::string, std::string> outputs;
  CellFFMetadata ff;
  bool unsupported = false;
};

class CellMap {
 public:
  static CellMap load(const std::string& path);

  std::optional<CellMapEntry> lookup(const std::string& raw_cell_type) const;

 private:
  std::vector<std::pair<std::string, CellMapEntry>> patterns_;
};

std::string normalize_cell_name(const std::string& raw);

GateType lookup_gate_type(const CellMap& map, const std::string& raw);

}  // namespace faultflow
