#include "scan/scan_chain.hpp"

#include <map>
#include <set>
#include <stdexcept>

namespace faultflow::scan {
namespace {

std::vector<ScanCell> scan_cells(const NormalizedGraph& ng) {
  std::vector<ScanCell> cells;
  for (const auto& [node_id, node] : ng.nodes) {
    if (node.type != NodeType::FF || !node.ff_config.has_scan) {
      continue;
    }
    cells.push_back({node_id, node.ff_config.scan_in_net,
                     node.ff_config.scan_enable_net,
                     node.ff_config.output_net});
  }
  return cells;
}

}  // namespace

std::vector<ScanChain> extract_scan_chains(const NormalizedGraph& ng) {
  const std::vector<ScanCell> cells = scan_cells(ng);
  if (cells.empty()) {
    return {};
  }

  std::map<int, ScanCell> by_node;
  std::map<int, int> node_by_scan_in;
  std::map<int, int> node_by_q;
  for (const ScanCell& cell : cells) {
    by_node[cell.node_id] = cell;
    if (node_by_scan_in.count(cell.scan_in_net)) {
      throw std::runtime_error("multiple scan FFs share one scan input net");
    }
    if (node_by_q.count(cell.q_net)) {
      throw std::runtime_error("multiple scan FFs share one Q net");
    }
    node_by_scan_in[cell.scan_in_net] = cell.node_id;
    node_by_q[cell.q_net] = cell.node_id;
  }

  std::vector<int> roots;
  for (const ScanCell& cell : cells) {
    if (!node_by_q.count(cell.scan_in_net)) {
      roots.push_back(cell.node_id);
    }
  }
  if (roots.empty()) {
    throw std::runtime_error("scan chain extraction found a cycle and no root");
  }

  std::set<int> visited;
  std::vector<ScanChain> chains;
  for (int root : roots) {
    ScanChain chain;
    int cur = root;
    while (true) {
      if (visited.count(cur)) {
        throw std::runtime_error("scan chain extraction found a cycle");
      }
      visited.insert(cur);
      const ScanCell cell = by_node.at(cur);
      if (chain.cells.empty()) {
        chain.scan_in_net = cell.scan_in_net;
        const auto net_it = ng.nets.find(chain.scan_in_net);
        if (net_it == ng.nets.end() || !net_it->second.is_pi) {
          throw std::runtime_error("scan chain root input is not a PI");
        }
      }
      chain.cells.push_back(cell);
      const auto next = node_by_scan_in.find(cell.q_net);
      if (next == node_by_scan_in.end()) {
        chain.scan_out_net = cell.q_net;
        const auto out_it = ng.nets.find(chain.scan_out_net);
        if (out_it == ng.nets.end() || !out_it->second.is_po) {
          throw std::runtime_error("scan chain terminal output is not a PO");
        }
        break;
      }
      cur = next->second;
    }
    chains.push_back(std::move(chain));
  }

  if (visited.size() != cells.size()) {
    throw std::runtime_error("disconnected scan FFs are not in any chain");
  }
  return chains;
}

}  // namespace faultflow::scan
