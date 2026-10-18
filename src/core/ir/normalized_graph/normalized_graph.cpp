#include "ir/normalized_graph/normalized_graph.hpp"

#include <algorithm>
#include <cctype>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>
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
  if (entry.ff.enable.present) {
    cfg.enable.present = true;
    cfg.enable.net = first_conn_or_throw(cell, entry.ff.enable.pin, cell.type);
    cfg.enable.polarity = entry.ff.enable.polarity;
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

// The top module's kUnobservedNetsAttr, checked against its output bits: an
// entry that names no output would silently leave a point observed that the
// caller meant to mask.
std::set<int> parse_unobserved_nets(const ParsedModule& mod,
                                    const std::set<int>& pos) {
  std::set<int> out;
  const auto it = mod.attrs.find(kUnobservedNetsAttr);
  if (it == mod.attrs.end()) {
    return out;
  }
  std::istringstream ids(it->second);
  std::string token;
  while (ids >> token) {
    size_t used = 0;
    int bit = 0;
    try {
      bit = std::stoi(token, &used);
    } catch (const std::exception&) {
      used = 0;
    }
    if (used != token.size()) {
      throw ParseError(std::string(kUnobservedNetsAttr) + ": not a net id: " + token);
    }
    if (!pos.count(bit)) {
      throw ParseError(std::string(kUnobservedNetsAttr) + ": net " + token +
                       " is not an output port bit");
    }
    out.insert(bit);
  }
  return out;
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
  // prefer the cell-map entry, then the direction Yosys wrote for the cell,
  // and only then guess from common output-pin names. A blackbox memory's
  // type is never in the cell map and its data output is named like "dout0",
  // which the name guess reads as an input -- leaving the output undriven
  // (a free variable to SAT, 0 to the simulator) and observed as a test point.
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
    const auto dir = cell.port_directions.find(pin);
    if (dir != cell.port_directions.end()) {
      return dir->second == "output";
    }
    return pin == "Y" || pin == "YS" || pin == "YC" || pin == "Q" ||
           pin == "X" || pin == "CO" || pin == "SUM" || pin == "S" ||
           pin == "YPAD" || pin == "DO" || pin == "HI" || pin == "LO";
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
  ng.unobserved = parse_unobserved_nets(mod, ng.POs);

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
        // Policy 1: EVERY output net of a blackboxed cell must be tagged so it
        // leaves the denominator. Use the direction-aware helper (same one
        // instance blackboxing uses) -- a bare pin-name whitelist missed
        // Sky130's dominant "X" output and conb's HI/LO.
        for (const auto& [pin, bits] : cell.conns) {
          if (is_output_pin(cell, pin)) {
            for (int bit : bits) {
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
          // Deferred entry without outputs metadata: fall back to the same
          // direction-aware helper (covers X / HI / LO, not just Y*/Q).
          for (const auto& [pin, bits] : cell.conns) {
            if (is_output_pin(cell, pin)) {
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

    if (entry->node_type == NodeType::ICG) {
      // An integrated clock gate: a gate node with a latch (eval_icg), its
      // inputs clock, enable and test enable in that order; a test pin the cell
      // has none of, or the netlist leaves unconnected, reads 0. The clock it
      // reads and the clock it drives are clock nets.
      const CellICGMetadata& icg = entry->icg;
      NormNode gate;
      gate.id = next_node_id++;
      gate.instance = inst;
      gate.type = NodeType::GATE;
      gate.gate_type = icg.edge == TriggerType::POSEDGE ? GateType::ICG_POS
                                                        : GateType::ICG_NEG;
      const auto view = cell.attrs.find(kIcgEnableAttr);
      if (view != cell.attrs.end()) {
        gate.gate_type =
            view->second == "chain" ? GateType::ICG_EN_CHAIN : GateType::ICG_EN;
      }
      gate.input_order = {icg.clock, icg.enable};
      if (!icg.test.empty()) {
        gate.input_order.push_back(icg.test);
      }
      for (const auto& pin : gate.input_order) {
        auto it = cell.conns.find(pin);
        if (it != cell.conns.end() && !it->second.empty()) {
          gate.input_pins[pin] = it->second.front();
        } else if (pin != icg.test) {
          throw ParseError("clock gate " + inst + " (type '" + cell.type +
                           "') has no connection on its pin " + pin);
        }
      }
      auto out = cell.conns.find(icg.output);
      if (out == cell.conns.end() || out->second.empty()) {
        continue;  // a clock gate clocking nothing
      }
      const int gclk = out->second.front();
      gate.output_pins[icg.output] = gclk;
      if (ng.nets[gclk].driver >= 0) {
        throw ParseError("Multiple drivers on net " + std::to_string(gclk));
      }
      ng.nets[gclk].driver = gate.id;
      const int clk = gate.input_pins.at(icg.clock);
      if (clk >= 0) {  // a constant (a view's tied-off root clock) is no clock
        tag_clock_net(ng, clk);
      }
      tag_clock_net(ng, gclk);
      ng.nodes[gate.id] = std::move(gate);
      continue;
    }

    if (entry->node_type == NodeType::CONST && !entry->constants.empty()) {
      // A tie cell with several outputs (Sky130's conb): a constant node per
      // output, each its own constant.
      for (const auto& [logical, lib_pin] : entry->outputs) {
        auto it = cell.conns.find(lib_pin);
        if (it == cell.conns.end() || it->second.empty()) {
          continue;
        }
        const auto value = entry->constants.find(logical);
        if (value == entry->constants.end()) {
          throw ParseError("tie cell " + cell.type + " has no constant for " + logical);
        }
        NormNode tie;
        tie.id = next_node_id++;
        tie.instance = inst;
        tie.type = NodeType::CONST;
        tie.gate_type = value->second ? GateType::CONST1 : GateType::CONST0;
        const int out_net = it->second.front();
        tie.output_pins[logical] = out_net;
        if (ng.nets[out_net].driver >= 0) {
          throw ParseError("Multiple drivers on net " + std::to_string(out_net));
        }
        ng.nets[out_net].driver = tie.id;
        ng.nodes[tie.id] = std::move(tie);
      }
      continue;
    }

    NormNode node;
    node.id = next_node_id++;
    node.instance = inst;
    node.type = entry->node_type;
    node.gate_type =
        entry->node_type == NodeType::FF ? GateType::DFF : entry->gate_type;

    if (entry->node_type == NodeType::GATE) {
      node.input_order = entry->inputs;
    }
    for (const auto& in_pin : entry->inputs) {
      auto it = cell.conns.find(in_pin);
      if (it != cell.conns.end() && !it->second.empty()) {
        node.input_pins[in_pin] = it->second.front();
      } else if (entry->node_type == NodeType::GATE) {
        // A declared input pin of a supported combinational gate that does not
        // resolve to a connection is a cell-map/compiler pin-name mismatch: the
        // input would silently become UNUSED and degrade the gate to a constant.
        // Fail loudly rather than produce wrong coverage. (FFs validate their
        // pins via build_ff_config.)
        throw ParseError("cell " + inst + " (type '" + cell.type +
                         "'): declared input pin '" + in_pin +
                         "' has no connection — cell-map/compiler pin-name "
                         "mismatch for this gate type");
      }
    }
    int qn_net = -1;
    for (const auto& [logical, lib_pin] : entry->outputs) {
      auto it = cell.conns.find(lib_pin);
      if (entry->node_type == NodeType::FF && logical == "QN") {
        // A flop's inverted output: driven by an inverter reading Q, below.
        if (it != cell.conns.end() && !it->second.empty()) {
          qn_net = it->second.front();
        }
        continue;
      }
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

    const int q_net = node.type == NodeType::FF ? node.ff_config.output_net : -1;
    const std::string node_instance = node.instance;
    ng.nodes[node.id] = std::move(node);
    if (qn_net >= 0) {
      // A flop's inverted output (the cell map's outputs.QN, Sky130's Q_N) is an
      // inverter reading Q, so the net has a driver, a value and fault sites like any
      // other -- a SimNode keeps one output.
      NormNode inv;
      inv.id = next_node_id++;
      inv.instance = node_instance + "$qn";
      inv.type = NodeType::GATE;
      inv.gate_type = GateType::INV;
      inv.input_pins["A"] = q_net;
      inv.input_order = {"A"};
      inv.output_pins["Y"] = qn_net;
      if (ng.nets[qn_net].driver >= 0) {
        throw ParseError("Multiple drivers on net " + std::to_string(qn_net));
      }
      ng.nets[qn_net].driver = inv.id;
      ng.nodes[inv.id] = std::move(inv);
    }
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

  // Levelization: level = 1 + max(driver levels); sources at 0.
  //
  // Single-pass Kahn longest-path in O(V + E). FF outputs and source nodes
  // (PIs, constants) are level 0; every other node is 1 + max(driver level).
  // FFs are treated as level-0 sources whose own inputs are ignored here, which
  // also breaks all sequential feedback so the combinational edge subgraph is a
  // DAG. This yields the identical node levels an iterate-to-fixpoint relaxation
  // converges to, but avoids its O(depth * V * log V) worst case — a real blowup
  // on deep, non-topologically-ordered netlists (e.g. a flattened multi-thousand-
  // level FIR) where std::map iteration order fights the dataflow order.
  std::unordered_map<int, int> indeg;
  std::unordered_map<int, int> level;
  std::unordered_map<int, std::vector<int>> consumers;
  indeg.reserve(ng.nodes.size() * 2);
  level.reserve(ng.nodes.size() * 2);
  consumers.reserve(ng.nodes.size() * 2);
  for (const auto& [nid, node] : ng.nodes) {
    (void)node;
    indeg[nid] = 0;
    level[nid] = 0;
  }
  for (const auto& [nid, node] : ng.nodes) {
    if (node.type == NodeType::FF) {
      continue;  // level-0 source; ignore inputs (also breaks sequential loops)
    }
    for (const auto& [pin, in_net] : node.input_pins) {
      (void)pin;
      const auto net_it = ng.nets.find(in_net);
      if (net_it == ng.nets.end()) {
        continue;
      }
      const int driver = net_it->second.driver;
      if (driver >= 0 && driver != nid && ng.nodes.count(driver)) {
        consumers[driver].push_back(nid);
        ++indeg[nid];
      }
    }
  }
  std::vector<int> ready;
  ready.reserve(ng.nodes.size());
  for (const auto& [nid, deg] : indeg) {
    if (deg == 0) {
      ready.push_back(nid);
    }
  }
  for (std::size_t head = 0; head < ready.size(); ++head) {
    const int u = ready[head];
    const int next_level = level[u] + 1;
    const auto cons_it = consumers.find(u);
    if (cons_it == consumers.end()) {
      continue;
    }
    for (const int v : cons_it->second) {
      if (next_level > level[v]) {
        level[v] = next_level;
      }
      if (--indeg[v] == 0) {
        ready.push_back(v);
      }
    }
  }
  for (auto& [nid, node] : ng.nodes) {
    node.level = level[nid];
  }

  return ng;
}

}  // namespace faultflow
