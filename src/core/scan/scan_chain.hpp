#pragma once

#include <vector>

#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow::scan {

struct ScanCell {
  int node_id = 0;
  int scan_in_net = -1;
  int scan_enable_net = -1;
  int q_net = -1;
};

struct ScanChain {
  std::vector<ScanCell> cells;
  int scan_in_net = -1;
  int scan_out_net = -1;
};

std::vector<ScanChain> extract_scan_chains(const NormalizedGraph& ng);

}  // namespace faultflow::scan
