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

struct CompiledSimGraph {
  std::vector<SimNode> nodes;
  std::vector<CompiledFFConfig> ff_configs;
  // An X-mode graph's latches (GateType LATCH, SimNode::ff_cfg): the enable's
  // level (enable_polarity), clear and preset. Never in ff_configs, which
  // pairs with ff_nodes.
  std::vector<CompiledFFConfig> latch_configs;
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
  // driver_index[net] = index of the node whose `out` == net, else -1. A pure
  // function of the (immutable) topology, so it is computed once at compile and
  // reused by the ATPG cone extraction instead of being rebuilt per fault.
  std::vector<int> driver_index;
  // Compiled for the X engine (NormalizedGraph::x_mode): it may hold nodes no
  // two-valued engine evaluates (GateType CONSTX).
  bool x_mode = false;
  // An X-mode graph with combinational loops (empty otherwise): in_loop[i] when
  // node i is on one; loop_levels[l] when level l holds one, which the X engines
  // settle to a fixed point (every loop is all in one level).
  std::vector<uint8_t> in_loop;
  std::vector<uint8_t> loop_levels;
};

std::string canonical_site_key(const CompiledSimGraph& cg, uint32_t cidx);

class GraphCompiler {
 public:
  static CompiledSimGraph compile(const class NormalizedGraph& ng);
};

}  // namespace faultflow
