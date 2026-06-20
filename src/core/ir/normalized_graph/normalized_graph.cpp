#include "ir/normalized_graph/normalized_graph.hpp"

#include <algorithm>
#include <cctype>
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

std::string lower_name(std::string name) {
  std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  return name;
}

bool is_clock_name(const std::string& name) {
  const std::string lower = lower_name(name);
  return lower == "clk" || lower == "clock" ||
         lower.find("clk") != std::string::npos;
}

bool is_reset_name(const std::string& name) {
  const std::string lower = lower_name(name);
  return lower == "rst" || lower == "reset" ||
         lower.find("reset") != std::string::npos ||
         lower.find("rst") != std::string::npos;
}

void tag_special_net(NormalizedGraph& ng, int bit, const std::string& name) {
  if (is_clock_name(name)) {
    ng.nets[bit].is_clock = true;
    ng.clocks.insert(bit);
  }
  if (is_reset_name(name)) {
    ng.nets[bit].is_reset = true;
    ng.resets.insert(bit);
  }
}

void tag_clock_net(NormalizedGraph& ng, int bit) {
  ng.nets[bit].is_clock = true;
  ng.clocks.insert(bit);
}

void tag_reset_net(NormalizedGraph& ng, int bit) {
  ng.nets[bit].is_reset = true;
  ng.resets.insert(bit);
}

int first_conn_or_throw(const ParsedCell& cell, const std::string& pin,
                        const std::string& cell_type) {
  const auto it = cell.conns.find(pin);
  if (it == cell.conns.end() || it->second.empty()) {
    throw ParseError("Cell " + cell_type + " missing required pin " + pin);
  }
  return it->second.front();
}

FFConfig build_ff_config(const CellMapEntry& entry, const ParsedCell& cell) {
  FFConfig cfg;
  cfg.trigger = entry.ff.trigger;
  cfg.clock_net = first_conn_or_throw(cell, entry.ff.clock, cell.type);
  cfg.data_net = first_conn_or_throw(cell, entry.ff.data, cell.type);
  const auto out_it = entry.outputs.find(entry.ff.output);
  if (out_it == entry.outputs.end()) {
    throw ParseError("FF cell map output missing: " + cell.type);
  }
  cfg.output_net = first_conn_or_throw(cell, out_it->second, cell.type);
  if (entry.ff.clear.present) {
    cfg.clear.present = true;
    cfg.clear.net = first_conn_or_throw(cell, entry.ff.clear.pin, cell.type);
    cfg.clear.polarity = entry.ff.clear.polarity;
    cfg.clear.value = entry.ff.clear.value;
  }
  if (entry.ff.preset.present) {
    cfg.preset.present = true;
    cfg.preset.net = first_conn_or_throw(cell, entry.ff.preset.pin, cell.type);
    cfg.preset.polarity = entry.ff.preset.polarity;
    cfg.preset.value = entry.ff.preset.value;
  }
  if (entry.ff.has_scan) {
    cfg.has_scan = true;
    cfg.scan_in_net = first_conn_or_throw(cell, entry.ff.scan_in, cell.type);
    cfg.scan_enable_net =
        first_conn_or_throw(cell, entry.ff.scan_enable, cell.type);
    cfg.scan_enable_polarity = entry.ff.scan_enable_polarity;
  }
  cfg.clear_preset_conflict_value = entry.ff.clear_preset_conflict_value;
  return cfg;
}

}  // namespace

NormalizedGraph NormalizedGraph::from_parsed(
    const ParsedGraph& parsed, const CellMap& cell_map,
    const std::string& policy,
    const std::set<std::string>& blackbox_instances) {
  NormalizedGraph ng;
  const ParsedModule& mod = parsed.top_module();
  std::map<int, int> parent;
  int next_node_id = 1;

  // Phase 9: validate every named blackbox instance exists (never silently
  // ignore a blackbox directive — Policy 1 spirit).
  for (const auto& name : blackbox_instances) {
    if (!mod.cells.count(name)) {
      throw ParseError("blackbox instance not found: " + name);
    }
  }

  // Resolve whether a pin is an output of a (possibly unknown-type) cell:
  // prefer the cell-map entry, else fall back to common output-pin names.
  auto is_output_pin = [&](const ParsedCell& cell,
                           const std::string& pin) -> bool {
    const auto entry = cell_map.lookup(cell.type);
    if (entry) {
      for (const auto& [logical, lib_pin] : entry->outputs) {
        (void)logical;
        if (lib_pin == pin) {
          return true;
        }
      }
      for (const auto& in_pin : entry->inputs) {
        if (in_pin == pin) {
          return false;
        }
      }
    }
    return pin == "Y" || pin == "YS" || pin == "YC" || pin == "Q" ||
           pin == "X" || pin == "CO" || pin == "SUM" || pin == "S" ||
           pin == "YPAD" || pin == "DO";
  };

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
      tag_special_net(ng, bit, name);
    }
  }

  for (const auto& [pname, port] : mod.ports) {
    for (int bit : port.bits) {
      ensure_net(bit);
      ng.nets[bit].names.push_back(pname);
      tag_special_net(ng, bit, pname);
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

    // Phase 9: instance blackboxing — model the cell as a test boundary.
    // Output nets become controllable pseudo-PIs; input nets become observable
    // pseudo-POs (TPs). The cell itself is NOT elaborated.
    if (blackbox_instances.count(inst)) {
      for (const auto& [pin, bits] : cell.conns) {
        const bool is_out = is_output_pin(cell, pin);
        for (int bit : bits) {
          if (is_out) {
            ng.nets[bit].is_pseudo_input = true;
            ng.pseudo_inputs.insert(bit);
          } else {
            ng.nets[bit].is_tp = true;
            ng.TPs.insert(bit);
          }
        }
      }
      ng.blackbox_instances.insert(inst);
      continue;
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
        if (!entry->outputs.empty()) {
          for (const auto& [logical, lib_pin] : entry->outputs) {
            (void)logical;
            auto it = cell.conns.find(lib_pin);
            if (it != cell.conns.end()) {
              for (int bit : it->second) {
                ng.nets[bit].is_blackboxed = true;
                ng.blackboxed.insert(bit);
              }
            }
          }
        } else {
          for (const auto& [pin, bits] : cell.conns) {
            if (pin == "Y" || pin == "YS" || pin == "YC" || pin == "Q" ||
                pin == "YPAD" || pin == "DO") {
              for (int bit : bits) {
                ng.nets[bit].is_blackboxed = true;
                ng.blackboxed.insert(bit);
              }
            }
          }
        }
        continue;
      }
      throw UnsupportedCellError(cell.type);
    }

    if (entry->node_type == NodeType::LATCH || entry->node_type == NodeType::TBUF) {
      throw UnsupportedCellError(cell.type);
    }

    NormNode node;
    node.id = next_node_id++;
    node.instance = inst;
    node.type = entry->node_type;
    node.gate_type =
        entry->node_type == NodeType::FF ? GateType::DFF : entry->gate_type;

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

    if (entry->node_type == NodeType::FF) {
      node.ff_config = build_ff_config(*entry, cell);
      tag_clock_net(ng, node.ff_config.clock_net);
      if (node.ff_config.clear.present) {
        tag_reset_net(ng, node.ff_config.clear.net);
      }
      if (node.ff_config.preset.present) {
        tag_reset_net(ng, node.ff_config.preset.net);
      }
      node.level = 0;
    }

    // IEEE 1500 wrapper boundary cell: record its core/sys nets (the node above
    // already drives the cell's output net, stable across modes). The cell
    // elaborates as an ordinary WBR_IN/WBR_OUT buffer node; only the per-mode
    // control/observe reconfiguration (build_mode_config) reads this record.
    if (entry->wbr.present) {
      auto core_it = cell.conns.find(entry->wbr.core_pin);
      auto sys_it = cell.conns.find(entry->wbr.sys_pin);
      if (core_it == cell.conns.end() || core_it->second.empty() ||
          sys_it == cell.conns.end() || sys_it->second.empty()) {
        throw ParseError("WBR cell missing core/sys pin connection: " + inst);
      }
      NormWrapperCell wc;
      wc.core_net = core_it->second.front();
      wc.sys_net = sys_it->second.front();
      wc.is_input = entry->wbr.is_input;
      ng.wrapper_cells.push_back(wc);
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

  // Phase 9: pseudo-PI source nodes for blackbox output nets (controllable).
  // A net that is also a real PI is already driven — skip it.
  for (int ppi : ng.pseudo_inputs) {
    if (ng.nets[ppi].driver >= 0) {
      continue;
    }
    NormNode ppi_node;
    ppi_node.id = next_node_id++;
    ppi_node.type = NodeType::GATE;
    ppi_node.gate_type = GateType::INPUT;
    ppi_node.output_pins["Y"] = ppi;
    ng.nets[ppi].driver = ppi_node.id;
    ng.nodes[ppi_node.id] = std::move(ppi_node);
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
      if (node.type == NodeType::FF) {
        node.level = 0;
        continue;
      }
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
