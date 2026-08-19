#pragma once

#include <map>
#include <optional>
#include <string>
#include <unordered_map>
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
  CellFFControl enable;  // sky130 edfxtp DE: synchronous D/hold mux
  uint8_t clear_preset_conflict_value = 0;
  bool present = false;
  bool has_scan = false;
};

// IEEE 1500 wrapper boundary cell metadata. `is_input` marks a cell on a core
// INPUT port (drives the core side) vs a core OUTPUT port (drives the system
// side). `core_pin`/`sys_pin` name the two functional data pins.
//
// When `scan` (Stage 4 native shiftable WBR), the cell is ALSO a scan FF
// (node_type == FF): it lowers into a scan-FF half (capturing `func_in_pin`,
// shifting via `chain_in_pin`/`chain_out_pin`) plus a mode-dependent output mux
// that drives the functional output net (`core_pin` for an input cell,
// `sys_pin` for an output cell) from the FF state `q` in INTEST/EXTEST. The five
// scan pins are required iff `scan`; they mirror the `ff:` block so either view
// is authoritative (cross-checked at parse time):
//   func_in_pin == ff.data, chain_in_pin == ff.scan_in,
//   chain_out_pin == ff.output (== CTO/q), scan_enable_pin == ff.scan_enable,
//   clock_pin == ff.clock.
struct CellWBRMetadata {
  bool present = false;
  bool is_input = false;
  std::string core_pin;
  std::string sys_pin;
  bool scan = false;
  std::string func_in_pin;      // CFI — functional input captured by the FF
  std::string chain_in_pin;     // CTI — wrapper-chain scan in
  std::string chain_out_pin;    // CTO — wrapper-chain scan out (== q)
  std::string scan_enable_pin;  // SE
  std::string clock_pin;        // CLK
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
  // Memoizes lookup() results per raw cell type. CellMap is immutable after
  // load(), so this is a pure cache: at CVA6 scale (~80 unique cell types over
  // 200k cells) it collapses the O(patterns) glob scan to one scan per distinct
  // type, then O(1) thereafter.
  mutable std::unordered_map<std::string, std::optional<CellMapEntry>>
      lookup_cache_;
};

std::string normalize_cell_name(const std::string& raw);

GateType lookup_gate_type(const CellMap& map, const std::string& raw);

}  // namespace faultflow
