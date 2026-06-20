#pragma once

#include <map>
#include <string>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

enum class SiteKind : uint8_t { STEM, BRANCH };

struct NetSiteInfo {
  SiteKind kind = SiteKind::STEM;
  std::string consumer_instance;
  std::string input_pin;
};

struct SimNode {
  GateType type = GateType::BUF;
  uint32_t in0 = UNUSED_INPUT;
  uint32_t in1 = UNUSED_INPUT;
  uint32_t in2 = UNUSED_INPUT;
  uint32_t in3 = UNUSED_INPUT;
  uint32_t in4 = UNUSED_INPUT;
  uint32_t in5 = UNUSED_INPUT;
  uint32_t out = 0;
  uint32_t ff_cfg = 0;
};

// IEEE 1500 wrapper boundary cell, in CompiledSimGraph (CompiledNetIndex)
// space. `core_idx` is the core-facing net, `sys_idx` the system-facing net.
// `is_input` marks a core-input cell (WBR_IN drives core_idx) vs a core-output
// cell (WBR_OUT drives sys_idx). Mode-agnostic; build_mode_config reads it.
struct CompiledWrapperCell {
  uint32_t core_idx = 0;
  uint32_t sys_idx = 0;
  bool is_input = false;
};

struct CompiledSimGraph {
  std::vector<SimNode> nodes;
  std::vector<CompiledFFConfig> ff_configs;
  std::vector<int> ff_nodes;
  std::vector<uint32_t> fanout_offsets;
  std::vector<uint32_t> fanout_targets;
  std::vector<int> level_starts;
  std::vector<int> observable;
  std::vector<int> pi_nets;
  // Controllable pseudo-PI nets (e.g. blackbox output nets driven by an INPUT
  // source node). A subset of pi_nets, not backed by a real module port.
  std::vector<int> pseudo_pi_nets;
  int net_count = 0;
  std::map<int, int> yosys_to_compiled;
  std::vector<int> compiled_to_yosys;
  std::vector<NetSiteInfo> net_sites;
  // IEEE 1500 wrapper boundary cells, in serial-WBR order. Mode-agnostic — the
  // topology (each WBR node drives a stable net) is unchanged across modes.
  std::vector<CompiledWrapperCell> wrapper_cells;
};

std::string canonical_site_key(const CompiledSimGraph& cg, uint32_t cidx);

// Per-wrapper-node hot-loop action under a given TestMode (indexed by the
// driven net's CompiledNetIndex). PASS = buffer (FUNCTIONAL passthrough);
// SKIP_STIMULUS = leave the broadcast value in place (control point);
// FORCE_ZERO = drive the inactive wrapper side to 0 (safe value).
enum class WbrAction : uint8_t { PASS = 0, SKIP_STIMULUS = 1, FORCE_ZERO = 2 };

// Mode-dependent control/observe reconfiguration derived from a CompiledSimGraph
// and a TestMode. Built ONCE per run (mode is fixed for a run) and reused — not
// recomputed per vector/batch. Pure function of (cg, mode); does not mutate cg.
struct ModeConfig {
  TestMode mode = TestMode::FUNCTIONAL;
  std::vector<uint32_t> stimulus_nets;    // broadcast writes test values here
  std::vector<uint32_t> observable_nets;  // observation points
  std::vector<uint32_t> safe_zero_nets;   // driven to 0 (inactive wrapper side)
  std::vector<uint8_t> wbr_action;        // per CompiledNetIndex: WbrAction
};

ModeConfig build_mode_config(const CompiledSimGraph& cg, TestMode mode);

class GraphCompiler {
 public:
  static CompiledSimGraph compile(const class NormalizedGraph& ng);
};

}  // namespace faultflow
