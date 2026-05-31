#pragma once

#include <map>
#include <vector>

#include "common/types.hpp"

namespace faultflow {

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
  std::vector<uint32_t> fanout_offsets;
  std::vector<uint32_t> fanout_targets;
  std::vector<int> level_starts;
  std::vector<int> observable;
  std::vector<int> pi_nets;
  int net_count = 0;
  std::map<int, int> yosys_to_compiled;
  std::vector<int> compiled_to_yosys;
};

class GraphCompiler {
 public:
  static CompiledSimGraph compile(const class NormalizedGraph& ng);
};

}  // namespace faultflow
