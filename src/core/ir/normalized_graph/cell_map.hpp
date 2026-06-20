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
  std::string scan_in;
  std::string scan_enable;
  TriggerType trigger = TriggerType::POSEDGE;
  Polarity scan_enable_polarity = Polarity::ACTIVE_HIGH;
  CellFFControl clear;
  CellFFControl preset;
  uint8_t clear_preset_conflict_value = 0;
  bool present = false;
  bool has_scan = false;
};

// IEEE 1500 wrapper boundary cell metadata. `is_input` marks a cell on a core
// INPUT port (drives the core side) vs a core OUTPUT port (drives the system
// side). `core_pin`/`sys_pin` name the two functional data pins.
struct CellWBRMetadata {
  bool present = false;
  bool is_input = false;
  std::string core_pin;
  std::string sys_pin;
};

struct CellMapEntry {
  NodeType node_type = NodeType::GATE;
  GateType gate_type = GateType::BUF;
  std::vector<std::string> inputs;
  std::map<std::string, std::string> outputs;
  CellFFMetadata ff;
  CellWBRMetadata wbr;
  bool unsupported = false;
  // Optional metadata-only nominal cell delay (e.g. ns). Read ONLY for
  // transition at-speed-relevance annotation/reporting — NEVER consumed by
  // gate-eval, normalization, or the SAT/CNF path (Policy: JSON is not
  // authoritative for logic semantics).
  std::optional<double> delay;
};

class CellMap {
 public:
  static CellMap load(const std::string& path);

  std::optional<CellMapEntry> lookup(const std::string& raw_cell_type) const;

  // Nominal delay metadata for a raw cell type, or nullopt if the cell is
  // unknown or carries no delay field. Reporting-only.
  std::optional<double> delay_for(const std::string& raw_cell_type) const;

 private:
  std::vector<std::pair<std::string, CellMapEntry>> patterns_;
};

std::string normalize_cell_name(const std::string& raw);

GateType lookup_gate_type(const CellMap& map, const std::string& raw);

}  // namespace faultflow
