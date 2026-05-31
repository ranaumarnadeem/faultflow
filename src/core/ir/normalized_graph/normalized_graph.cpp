#include "ir/normalized_graph/normalized_graph.hpp"

#include <algorithm>
#include "common/errors.hpp"

namespace faultflow {
namespace {

int canonicalize(int id, std::map<int, int>& parent) {
  if (!parent.count(id)) {
    parent[id] = id;
  }
  if (parent[id] != id) {
    parent[id] = canonicalize(parent[id], parent);
  }
  return parent[id];
}

void unite(int a, int b, std::map<int, int>& parent) {
  a = canonicalize(a, parent);
  b = canonicalize(b, parent);
  if (a != b) {
    parent[b] = a;
  }
}

}  // namespace

NormalizedGraph NormalizedGraph::from_parsed(const ParsedGraph& parsed,
                                             const CellMap& cell_map,
                                             const std::string& policy) {
  NormalizedGraph ng;
  const ParsedModule& mod = parsed.top_module();
  std::map<int, int> parent;
  int next_node_id = 1;

  auto ensure_net = [&](int raw_id) {
    if (!ng.nets.count(raw_id)) {
      NormNet n;
      n.id = raw_id;
      n.canonical_id = raw_id;
      ng.nets[raw_id] = n;
    }
  };

  // Collect nets and alias same-bit groups from netnames
  for (const auto& [name, net] : mod.netnames) {
    for (int bit : net.bits) {
      ensure_net(bit);
      ng.nets[bit].names.push_back(name);
    }
    if (net.bits.size() > 1) {
      for (size_t i = 1; i < net.bits.size(); ++i) {
        unite(net.bits[0], net.bits[i], parent);
      }
    }
  }

  for (const auto& [pname, port] : mod.ports) {
    for (int bit : port.bits) {
      ensure_net(bit);
      ng.nets[bit].names.push_back(pname);
      if (port.direction == "input") {
        ng.nets[bit].is_pi = true;
        ng.PIs.insert(bit);
      } else if (port.direction == "output") {
        ng.nets[bit].is_po = true;
        ng.POs.insert(bit);
      }
    }
  }

  for (const auto& [inst, cell] : mod.cells) {
    for (const auto& [pin, bits] : cell.conns) {
      for (int bit : bits) {
        ensure_net(bit);
      }
    }

    const auto entry = cell_map.lookup(cell.type);
    if (!entry) {
      if (policy == "blackbox") {
        for (const auto& [pin, bits] : cell.conns) {
          for (int bit : bits) {
            if (pin == "Y" || pin == "YS" || pin == "YC" || pin == "Q") {
              ng.nets[bit].is_blackboxed = true;
              ng.blackboxed.insert(bit);
            }
          }
        }
        continue;
      }
      throw UnsupportedCellError(cell.type);
    }

    if (entry->unsupported) {
      if (policy == "blackbox") {
        for (const auto& [pin, bits] : cell.conns) {
          for (int bit : bits) {
            ng.nets[bit].is_blackboxed = true;
            ng.blackboxed.insert(bit);
          }
        }
        continue;
      }
      throw UnsupportedCellError(cell.type);
    }

    if (entry->node_type == NodeType::FF || entry->node_type == NodeType::LATCH ||
        entry->node_type == NodeType::TBUF) {
      throw UnsupportedCellError(cell.type);
    }

    NormNode node;
    node.id = next_node_id++;
    node.type = entry->node_type;
    node.gate_type = entry->gate_type;

    for (const auto& in_pin : entry->inputs) {
      auto it = cell.conns.find(in_pin);
      if (it != cell.conns.end() && !it->second.empty()) {
        node.input_pins[in_pin] = it->second.front();
      }
    }
    for (const auto& [logical, lib_pin] : entry->outputs) {
      auto it = cell.conns.find(lib_pin);
      if (it != cell.conns.end() && !it->second.empty()) {
        const int out_net = it->second.front();
        node.output_pins[logical] = out_net;
        if (ng.nets[out_net].driver >= 0) {
          throw ParseError("Multiple drivers on net " + std::to_string(out_net));
        }
        ng.nets[out_net].driver = node.id;
      }
    }

    ng.nodes[node.id] = std::move(node);
  }

  // PI source pseudo-nodes (INPUT)
  for (int pi : ng.PIs) {
    NormNode pi_node;
    pi_node.id = next_node_id++;
    pi_node.type = NodeType::GATE;
    pi_node.gate_type = GateType::INPUT;
    pi_node.output_pins["Y"] = pi;
    if (ng.nets[pi].driver >= 0) {
      throw ParseError("PI net already has driver");
    }
    ng.nets[pi].driver = pi_node.id;
    ng.nodes[pi_node.id] = std::move(pi_node);
  }

  // CONST drivers (only when referenced)
  auto add_const = [&](int const_id, GateType gt) {
    NormNode cnode;
    cnode.id = next_node_id++;
    cnode.type = NodeType::CONST;
    cnode.gate_type = gt;
    cnode.output_pins["Y"] = const_id;
    ng.nets[const_id].driver = cnode.id;
    ng.nodes[cnode.id] = std::move(cnode);
  };
  if (ng.nets.count(CONST0_NET_ID)) {
    add_const(CONST0_NET_ID, GateType::CONST0);
  }
  if (ng.nets.count(CONST1_NET_ID)) {
    add_const(CONST1_NET_ID, GateType::CONST1);
  }

  // Apply aliases
  std::map<int, std::vector<int>> groups;
  for (auto& [id, net] : ng.nets) {
    const int c = canonicalize(id, parent);
    groups[c].push_back(id);
  }
  for (auto& [c, ids] : groups) {
    std::sort(ids.begin(), ids.end());
    const int canonical = ids.front();
    ng.alias_map[canonical] = ids;
    for (int id : ids) {
      ng.nets[id].canonical_id = canonical;
      for (const auto& n : ng.nets[id].names) {
        ng.name_map[canonical].push_back(n);
      }
    }
  }

  // Fanout from nodes
  for (const auto& [nid, node] : ng.nodes) {
    for (const auto& [pin, in_net] : node.input_pins) {
      (void)pin;
      ng.nets[in_net].fanout.push_back(nid);
    }
  }

  // Levelization: level = 1 + max(driver levels); sources at 0
  bool changed = true;
  int guard = 0;
  while (changed && guard++ < static_cast<int>(ng.nodes.size()) + 5) {
    changed = false;
    for (auto& [nid, node] : ng.nodes) {
      int max_in = -1;
      for (const auto& [pin, in_net] : node.input_pins) {
        (void)pin;
        const int driver = ng.nets[in_net].driver;
        if (driver >= 0 && ng.nodes.count(driver)) {
          max_in = std::max(max_in, ng.nodes[driver].level);
        }
      }
      const int new_level = (max_in < 0) ? 0 : max_in + 1;
      if (node.level != new_level) {
        node.level = new_level;
        changed = true;
      }
    }
  }

  return ng;
}

}  // namespace faultflow
