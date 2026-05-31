#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

struct CellMapEntry {
  NodeType node_type = NodeType::GATE;
  GateType gate_type = GateType::BUF;
  std::vector<std::string> inputs;
  std::map<std::string, std::string> outputs;
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
