#include <stdexcept>

#include "ir/compiled_graph/compiled_graph.hpp"

#include <algorithm>

#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow {
namespace {

uint32_t map_net(int yosys_id, std::map<int, int>& y2c, std::vector<int>& c2y) {
  auto it = y2c.find(yosys_id);
  if (it != y2c.end()) {
    return static_cast<uint32_t>(it->second);
  }
  const int idx = static_cast<int>(c2y.size());
  y2c[yosys_id] = idx;
  c2y.push_back(yosys_id);
  return static_cast<uint32_t>(idx);
}

uint32_t append_branch_alias(std::vector<int>& c2y, int source_yosys_id) {
  const uint32_t idx = static_cast<uint32_t>(c2y.size());
  c2y.push_back(source_yosys_id);
  return idx;
}

void wire_inputs(SimNode& sn, GateType gt,
                 const std::map<std::string, int>& pins,
                 std::map<int, int>& y2c, std::vector<int>& c2y) {
  auto wire = [&](const char* name, uint32_t& slot) {
    auto it = pins.find(name);
    if (it != pins.end()) {
      slot = map_net(it->second, y2c, c2y);
    }
  };
  auto wire_seq = [&](std::initializer_list<const char*> names) {
    uint32_t* slots[] = {&sn.in0, &sn.in1, &sn.in2,
                         &sn.in3, &sn.in4, &sn.in5};
    size_t idx = 0;
    for (const char* name : names) {
      if (idx < 6) {
        wire(name, *slots[idx]);
      }
      ++idx;
    }
  };
  switch (gt) {
    case GateType::INV:
    case GateType::BUF:
      wire("A", sn.in0);
      break;
    case GateType::INPUT:
    case GateType::DFF:
      break;
    case GateType::AND2:
    case GateType::OR2:
    case GateType::NAND2:
    case GateType::NOR2:
    case GateType::XOR2:
    case GateType::XNOR2:
    case GateType::NAND2B:
    case GateType::NOR2B:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("A_N", sn.in0);
      wire("B_N", sn.in1);
      wire("SLEEP", sn.in1);  // lpflow_inputiso1p (OR2)
      break;
    case GateType::AND2B:
      // in0 & ~in1: the true input first, the bubbled one second -- sky130
      // and2b's B and A_N, lpflow_isobufsrc's A and SLEEP.
      wire("A", sn.in0);
      wire("B", sn.in0);
      wire("A_N", sn.in1);
      wire("SLEEP", sn.in1);
      break;
    case GateType::AND3:
    case GateType::OR3:
    case GateType::XOR3:
    case GateType::XNOR3:
    case GateType::NAND3B:
    case GateType::NOR3B:
    case GateType::OR3B:
      wire_seq({"A", "B", "C"});
      wire("A_N", sn.in0);
      wire("C_N", sn.in2);
      break;
    case GateType::AND4:
    case GateType::OR4:
    case GateType::NAND4:
    case GateType::NOR4:
    case GateType::NAND4B:
    case GateType::NOR4B:
    case GateType::OR4B:
      wire_seq({"A", "B", "C", "D"});
      wire("A_N", sn.in0);
      wire("D_N", sn.in3);
      break;
    case GateType::NAND3:
    case GateType::NOR3:
    case GateType::AOI21:
    case GateType::OAI21:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);
      break;
    case GateType::AOI22:
    case GateType::OAI22:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);
      wire("D", sn.in3);
      break;
    case GateType::MUX2:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("S", sn.in2);
      wire("S0", sn.in2);  // OSU035 MX2X* (OpenTestability's control points)
      break;
    case GateType::MUX2_NI:
      wire_seq({"A0", "A1", "S"});
      break;
    case GateType::A21O:
    case GateType::A21OI:
    case GateType::A21BO:
    case GateType::A21BOI:
      wire_seq({"A1", "A2", "B1"});
      wire("B1_N", sn.in2);
      break;
    case GateType::A22O:
    case GateType::A22OI:
    case GateType::O22A:
    case GateType::O22AI:
      wire_seq({"A1", "A2", "B1", "B2"});
      break;
    case GateType::A211O:
    case GateType::A211OI:
    case GateType::O211A:
    case GateType::O211AI:
      wire_seq({"A1", "A2", "B1", "C1"});
      break;
    case GateType::A2111OI:
    case GateType::A2111O:
      wire_seq({"A1", "A2", "B1", "C1", "D1"});
      break;
    case GateType::A221O:
    case GateType::A221OI:
    case GateType::O221A:
    case GateType::O221AI:
      wire_seq({"A1", "A2", "B1", "B2", "C1"});
      break;
    case GateType::A31O:
    case GateType::A31OI:
    case GateType::O31A:
    case GateType::O31AI:
      wire_seq({"A1", "A2", "A3", "B1"});
      break;
    case GateType::A32O:
    case GateType::A32OI:
    case GateType::O32AI:
    case GateType::O32A:
      wire_seq({"A1", "A2", "A3", "B1", "B2"});
      break;
    case GateType::A41O:
    case GateType::A41OI:
    case GateType::O41A:
    case GateType::O41AI:
      wire_seq({"A1", "A2", "A3", "A4", "B1"});
      break;
    case GateType::O21A:
    case GateType::O21AI:
    case GateType::O21BA:
    case GateType::O21BAI:
      wire_seq({"A1", "A2", "B1"});
      wire("B1_N", sn.in2);
      break;
    case GateType::O311A:
    case GateType::O311AI:
      wire_seq({"A1", "A2", "A3", "B1", "C1"});
      break;
    case GateType::A311O:
    case GateType::A311OI:
      wire_seq({"A1", "A2", "A3", "B1", "C1"});
      break;
    case GateType::A222OI:
    case GateType::A222O:
    case GateType::O222AI:
      wire_seq({"A1", "A2", "B1", "B2", "C1", "C2"});
      break;
    case GateType::O33AI:
      wire_seq({"A1", "A2", "A3", "B1", "B2", "B3"});
      break;
    case GateType::A2BB2OI:
    case GateType::O2BB2AI:
    case GateType::O2BB2A:
      wire("A1_N", sn.in0);
      wire("A2_N", sn.in1);
      wire("B1", sn.in2);
      wire("B2", sn.in3);
      break;
    case GateType::AND3B:
      wire("A_N", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);
      break;
    case GateType::AND4B:
      wire("A_N", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);
      wire("D", sn.in3);
      break;
    case GateType::MUX2I:
      wire_seq({"A0", "A1", "S"});
      break;
    case GateType::NOR4BB:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C_N", sn.in2);
      wire("D_N", sn.in3);
      break;
    case GateType::NAND4BB:
    case GateType::AND4BB:
      wire("A_N", sn.in0);
      wire("B_N", sn.in1);
      wire("C", sn.in2);
      wire("D", sn.in3);
      break;
    case GateType::OR2B:
      wire("A", sn.in0);
      wire("B_N", sn.in1);
      break;
    case GateType::OR4BB:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C_N", sn.in2);
      wire("D_N", sn.in3);
      break;
    case GateType::A2BB2O:
      wire("A1_N", sn.in0);
      wire("A2_N", sn.in1);
      wire("B1", sn.in2);
      wire("B2", sn.in3);
      break;
    case GateType::MUX4:
      wire_seq({"A0", "A1", "A2", "A3", "S0", "S1"});
      break;
    case GateType::O2111A:
    case GateType::O2111AI:
      wire_seq({"A1", "A2", "B1", "C1", "D1"});
      break;
    case GateType::ADDF_S:
    case GateType::ADDF_CO:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);    // OSU035 FAX1
      wire("CIN", sn.in2);  // sky130 fa
      break;
    case GateType::ADDH_S:
    case GateType::ADDH_CO:
      wire("A", sn.in0);
      wire("B", sn.in1);
      break;
    case GateType::CONST0:
    case GateType::CONST1:
      break;
    default:
      break;
  }
}

// A gate's inputs, wired to in0, in1, ... in its cell-map entry's order -- the
// contract a cell map states ("inputs" is ordered), whatever its pins are named. A
// node without that order (none the cell map gave) is wired by pin name.
void wire_node_inputs(SimNode& sn, GateType gt, const NormNode& node,
                      std::map<int, int>& y2c, std::vector<int>& c2y) {
  if (node.input_order.empty()) {
    wire_inputs(sn, gt, node.input_pins, y2c, c2y);
    return;
  }
  uint32_t* slots[] = {&sn.in0, &sn.in1, &sn.in2, &sn.in3, &sn.in4, &sn.in5};
  if (node.input_order.size() > 6) {
    throw std::runtime_error("gate " + node.instance + " has more than six inputs");
  }
  for (size_t k = 0; k < node.input_order.size(); ++k) {
    const auto it = node.input_pins.find(node.input_order[k]);
    if (it != node.input_pins.end()) {
      *slots[k] = map_net(it->second, y2c, c2y);
    }
  }
}

struct FanoutEdge {
  size_t node_idx = 0;
  int slot = 0;
};

uint32_t* input_slot(SimNode& sn, int slot) {
  switch (slot) {
    case 0:
      return &sn.in0;
    case 1:
      return &sn.in1;
    case 2:
      return &sn.in2;
    case 3:
      return &sn.in3;
    case 4:
      return &sn.in4;
    case 5:
      return &sn.in5;
    default:
      return nullptr;
  }
}

std::string pin_for_compiled_input(const NormNode& norm,
                                   const std::map<int, int>& y2c,
                                   uint32_t compiled_in) {
  for (const auto& [pin, yid] : norm.input_pins) {
    const auto it = y2c.find(yid);
    if (it != y2c.end() &&
        static_cast<uint32_t>(it->second) == compiled_in) {
      return pin;
    }
  }
  return "";
}

// The pin a fanout edge reads: a gate's by the slot wire_node_inputs wired it to, so
// two of its pins on one stem are two sites. A flop's slots name no pin: its pins on
// one stem (D and the scan in, in a shift register) share the first's site.
std::string pin_of_edge(const NormNode& norm, int slot,
                        const std::map<int, int>& y2c, uint32_t compiled_in) {
  if (static_cast<size_t>(slot) < norm.input_order.size()) {
    const std::string& pin = norm.input_order[slot];
    const auto wired = norm.input_pins.find(pin);
    if (wired != norm.input_pins.end()) {
      const auto it = y2c.find(wired->second);
      if (it != y2c.end() && static_cast<uint32_t>(it->second) == compiled_in) {
        return pin;
      }
    }
  }
  return pin_for_compiled_input(norm, y2c, compiled_in);
}

void split_fanout_branches(CompiledSimGraph& cg, const NormalizedGraph& ng,
                           const std::vector<int>& sim_owner_norm_id,
                           std::map<int, int>& y2c, std::vector<int>& c2y,
                           std::vector<int>& node_levels) {
  const uint32_t net_count = static_cast<uint32_t>(c2y.size());
  std::vector<std::vector<FanoutEdge>> fanout_edges(net_count);

  for (size_t ni = 0; ni < cg.nodes.size(); ++ni) {
    const SimNode& sn = cg.nodes[ni];
    for (int slot = 0; slot < 6; ++slot) {
      const uint32_t* in = input_slot(const_cast<SimNode&>(sn), slot);
      if (in != nullptr && *in != UNUSED_INPUT) {
        fanout_edges[*in].push_back({ni, slot});
      }
    }
  }

  cg.net_sites.assign(net_count, NetSiteInfo{});
  // A branch into flops is evaluated after every gate, so it holds its stem's value
  // of the same evaluation: the flops read it only at their update -- a clock a
  // gate drives (an integrated clock gate's), and what an edge latches, sampled as
  // it settled the cycle before (SimState::settled). The rule below, a branch one
  // level before its consumer, would put a branch into a flop (level 0) before
  // every gate, a pass behind its stem.
  const int after_all =
      node_levels.empty()
          ? 0
          : *std::max_element(node_levels.begin(), node_levels.end()) + 1;

  for (uint32_t stem = 0; stem < net_count; ++stem) {
    if (fanout_edges[stem].size() <= 1) {
      continue;
    }
    // Group fanout edges by the physical consumer pin: (owner norm-node id,
    // input pin). A multi-output cell (e.g. a full adder) is compiled into
    // several SimNodes (ADDF_S, ADDF_CO) that each read the same external input
    // on ONE physical pin. Those sub-node edges must collapse to a SINGLE branch
    // so the pin stays one fault site; otherwise both branches carry the same
    // (instance, pin) and their canonical site keys collide. Edges with no
    // resolvable owner/pin keep one branch each (unchanged behaviour).
    struct BranchGroup {
      std::string instance;
      std::string input_pin;
      std::vector<FanoutEdge> edges;
    };
    std::vector<BranchGroup> groups;
    std::map<std::pair<int, std::string>, size_t> group_index;
    for (const FanoutEdge& edge : fanout_edges[stem]) {
      int norm_id = -1;
      std::string instance;
      std::string input_pin;
      if (edge.node_idx < sim_owner_norm_id.size()) {
        norm_id = sim_owner_norm_id[edge.node_idx];
        if (ng.nodes.count(norm_id)) {
          const NormNode& norm = ng.nodes.at(norm_id);
          input_pin = pin_of_edge(norm, edge.slot, y2c, stem);
          instance = norm.instance;
        }
      }
      if (norm_id >= 0 && !input_pin.empty()) {
        const auto key = std::make_pair(norm_id, input_pin);
        auto it = group_index.find(key);
        if (it == group_index.end()) {
          group_index.emplace(key, groups.size());
          groups.push_back({instance, input_pin, {edge}});
        } else {
          groups[it->second].edges.push_back(edge);
        }
      } else {
        groups.push_back({std::string(), std::string(), {edge}});
      }
    }

    for (const BranchGroup& group : groups) {
      const int consumer_level =
          static_cast<int>(node_levels[group.edges.front().node_idx]);
      const int source_yosys_id = c2y.at(stem);
      const uint32_t branch = append_branch_alias(c2y, source_yosys_id);

      if (!group.instance.empty() && !group.input_pin.empty()) {
        if (branch >= cg.net_sites.size()) {
          cg.net_sites.resize(branch + 1);
        }
        NetSiteInfo site;
        site.kind = SiteKind::BRANCH;
        site.consumer_instance = group.instance;
        site.input_pin = group.input_pin;
        cg.net_sites[branch] = std::move(site);
      }

      SimNode buf;
      buf.type = GateType::BUF;
      buf.in0 = stem;
      buf.out = branch;
      const bool into_flops = std::all_of(
          group.edges.begin(), group.edges.end(), [&](const FanoutEdge& edge) {
            return cg.nodes[edge.node_idx].type == GateType::DFF;
          });
      node_levels.push_back(into_flops ? after_all : std::max(0, consumer_level - 1));
      cg.nodes.push_back(buf);

      for (const FanoutEdge& edge : group.edges) {
        uint32_t* target = input_slot(cg.nodes[edge.node_idx], edge.slot);
        if (target != nullptr) {
          *target = branch;
        }
      }
    }
  }
}

// The strongly connected components of the nodes: an edge from a net's driver to
// each node reading it, none into a flop (a loop through one is sequential, not
// combinational). `comp[i]` numbers node i's component in topological order (an
// edge never goes to a lower number); `cyclic[c]`: c is a loop -- more than one
// node, or one reading its own output. Iterative Tarjan.
struct Components {
  std::vector<int> comp;
  std::vector<char> cyclic;
  int count = 0;
  std::vector<std::vector<int>> succ;
};

Components strongly_connected(const CompiledSimGraph& cg, size_t net_count) {
  const size_t n = cg.nodes.size();
  Components out;
  std::vector<int> driver(net_count, -1);
  for (size_t i = 0; i < n; ++i) {
    if (cg.nodes[i].out < net_count) {
      driver[cg.nodes[i].out] = static_cast<int>(i);
    }
  }
  out.succ.assign(n, {});
  std::vector<char> self(n, 0);
  for (size_t v = 0; v < n; ++v) {
    const SimNode& sn = cg.nodes[v];
    if (sn.type == GateType::DFF || sn.type == GateType::INPUT) {
      continue;
    }
    for (uint32_t slot : {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5}) {
      if (slot == UNUSED_INPUT || slot >= net_count || driver[slot] < 0) {
        continue;
      }
      const auto u = static_cast<size_t>(driver[slot]);
      out.succ[u].push_back(static_cast<int>(v));
      self[v] = static_cast<char>(self[v] || u == v);
    }
  }
  std::vector<int> index(n, -1);
  std::vector<int> low(n, 0);
  std::vector<char> on_stack(n, 0);
  std::vector<int> stack;
  std::vector<std::vector<int>> found;  // components, sinks first
  int next = 0;
  for (size_t root = 0; root < n; ++root) {
    if (index[root] >= 0) {
      continue;
    }
    std::vector<std::pair<int, size_t>> work{{static_cast<int>(root), 0}};
    index[root] = low[root] = next++;
    stack.push_back(static_cast<int>(root));
    on_stack[root] = 1;
    while (!work.empty()) {
      auto& [v, edge] = work.back();
      if (edge < out.succ[static_cast<size_t>(v)].size()) {
        const int w = out.succ[static_cast<size_t>(v)][edge++];
        if (index[static_cast<size_t>(w)] < 0) {
          index[static_cast<size_t>(w)] = low[static_cast<size_t>(w)] = next++;
          stack.push_back(w);
          on_stack[static_cast<size_t>(w)] = 1;
          work.emplace_back(w, 0);
        } else if (on_stack[static_cast<size_t>(w)] != 0) {
          low[static_cast<size_t>(v)] =
              std::min(low[static_cast<size_t>(v)], index[static_cast<size_t>(w)]);
        }
        continue;
      }
      const int done = v;
      work.pop_back();
      if (!work.empty()) {
        const int parent = work.back().first;
        low[static_cast<size_t>(parent)] =
            std::min(low[static_cast<size_t>(parent)], low[static_cast<size_t>(done)]);
      }
      if (low[static_cast<size_t>(done)] == index[static_cast<size_t>(done)]) {
        std::vector<int> members;
        int w = -1;
        do {
          w = stack.back();
          stack.pop_back();
          on_stack[static_cast<size_t>(w)] = 0;
          members.push_back(w);
        } while (w != done);
        found.push_back(std::move(members));
      }
    }
  }
  out.count = static_cast<int>(found.size());
  out.comp.assign(n, 0);
  out.cyclic.assign(found.size(), 0);
  for (size_t k = 0; k < found.size(); ++k) {
    const int c = out.count - 1 - static_cast<int>(k);  // topological number
    for (int v : found[k]) {
      out.comp[static_cast<size_t>(v)] = c;
    }
    out.cyclic[static_cast<size_t>(c)] =
        static_cast<char>(found[k].size() > 1 || self[static_cast<size_t>(found[k][0])]);
  }
  return out;
}

// An X-mode graph with a loop: every node's level from its component -- a
// component's level one above the highest it reads from, a loop all in one level,
// which the X engines settle to a fixed point. Leaves an acyclic graph's levels
// as they are (returns false).
bool loop_levels_for(const CompiledSimGraph& cg, size_t net_count,
                     std::vector<int>& node_levels) {
  const Components c = strongly_connected(cg, net_count);
  if (std::none_of(c.cyclic.begin(), c.cyclic.end(), [](char x) { return x != 0; })) {
    return false;
  }
  std::vector<std::vector<int>> members(static_cast<size_t>(c.count));
  for (size_t v = 0; v < c.comp.size(); ++v) {
    members[static_cast<size_t>(c.comp[v])].push_back(static_cast<int>(v));
  }
  std::vector<int> level(static_cast<size_t>(c.count), 0);
  for (int k = 0; k < c.count; ++k) {  // topological order
    for (int u : members[static_cast<size_t>(k)]) {
      for (int v : c.succ[static_cast<size_t>(u)]) {
        const int cv = c.comp[static_cast<size_t>(v)];
        if (cv != k) {
          level[static_cast<size_t>(cv)] =
              std::max(level[static_cast<size_t>(cv)], level[static_cast<size_t>(k)] + 1);
        }
      }
    }
  }
  for (size_t v = 0; v < node_levels.size(); ++v) {
    node_levels[v] = level[static_cast<size_t>(c.comp[v])];
  }
  return true;
}

// After the nodes are in level order: which node is on a loop, and which level
// holds one (CompiledSimGraph::in_loop, loop_levels).
void flag_loops(CompiledSimGraph& cg, size_t net_count) {
  const Components c = strongly_connected(cg, net_count);
  cg.in_loop.assign(cg.nodes.size(), 0);
  for (size_t v = 0; v < cg.nodes.size(); ++v) {
    cg.in_loop[v] = static_cast<uint8_t>(c.cyclic[static_cast<size_t>(c.comp[v])]);
  }
  cg.loop_levels.assign(cg.level_starts.empty() ? 0 : cg.level_starts.size() - 1, 0);
  for (size_t l = 0; l < cg.loop_levels.size(); ++l) {
    for (int i = cg.level_starts[l]; i < cg.level_starts[l + 1]; ++i) {
      if (cg.in_loop[static_cast<size_t>(i)] != 0) {
        cg.loop_levels[l] = 1;
      }
    }
  }
}

void rebuild_level_starts(CompiledSimGraph& cg,
                          const std::vector<int>& node_levels) {
  cg.level_starts.clear();
  if (cg.nodes.empty()) {
    return;
  }
  std::vector<size_t> order(cg.nodes.size());
  for (size_t i = 0; i < order.size(); ++i) {
    order[i] = i;
  }
  std::stable_sort(order.begin(), order.end(),
                   [&](size_t a, size_t b) {
                     if (node_levels[a] != node_levels[b]) {
                       return node_levels[a] < node_levels[b];
                     }
                     return a < b;
                   });

  std::vector<SimNode> sorted;
  std::vector<int> sorted_levels;
  sorted.reserve(cg.nodes.size());
  sorted_levels.reserve(cg.nodes.size());
  for (size_t idx : order) {
    sorted.push_back(cg.nodes[idx]);
    sorted_levels.push_back(node_levels[idx]);
  }
  cg.nodes = std::move(sorted);
  cg.ff_nodes.clear();

  int cur = sorted_levels[0];
  cg.level_starts.push_back(0);
  for (size_t i = 1; i < sorted_levels.size(); ++i) {
    if (sorted_levels[i] != cur) {
      cg.level_starts.push_back(static_cast<int>(i));
      cur = sorted_levels[i];
    }
  }
  cg.level_starts.push_back(static_cast<int>(cg.nodes.size()));
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    if (cg.nodes[i].type == GateType::DFF) {
      cg.ff_nodes.push_back(static_cast<int>(i));
    }
  }
}

void rebuild_fanout_csr(CompiledSimGraph& cg) {
  std::vector<std::vector<uint32_t>> fanout_lists(cg.net_count);
  for (const auto& sn : cg.nodes) {
    const uint32_t ins[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5};
    for (uint32_t in : ins) {
      if (in != UNUSED_INPUT) {
        fanout_lists[in].push_back(sn.out);
      }
    }
  }
  cg.fanout_offsets.clear();
  cg.fanout_targets.clear();
  cg.fanout_offsets.push_back(0);
  for (const auto& fo : fanout_lists) {
    cg.fanout_targets.insert(cg.fanout_targets.end(), fo.begin(), fo.end());
    cg.fanout_offsets.push_back(
        static_cast<uint32_t>(cg.fanout_targets.size()));
  }
}

// A cell missing from the cell map (unsupported_cells = blackbox) has unknown
// outputs (ng.blackboxed): the simulators read them as 0 and SAT as free, and
// neither is what the silicon does. Nothing may be credited through them, so
// every observable their value reaches, through gates and flops (the fanout
// CSR runs D, clock and controls to Q), leaves `observable`; the graders, the
// golden reference and every SAT miter read only that set.
void drop_unknown_reach(CompiledSimGraph& cg, const NormalizedGraph& ng) {
  if (ng.blackboxed.empty() || cg.observable.empty()) {
    return;
  }
  std::vector<char> reached(static_cast<size_t>(cg.net_count), 0);
  std::vector<uint32_t> work;
  for (int yid : ng.blackboxed) {
    const auto it = cg.yosys_to_compiled.find(yid);
    if (it != cg.yosys_to_compiled.end() && !reached[it->second]) {
      reached[it->second] = 1;
      work.push_back(static_cast<uint32_t>(it->second));
    }
  }
  while (!work.empty()) {
    const uint32_t net = work.back();
    work.pop_back();
    for (uint32_t i = cg.fanout_offsets[net]; i < cg.fanout_offsets[net + 1];
         ++i) {
      const uint32_t t = cg.fanout_targets[i];
      if (!reached[t]) {
        reached[t] = 1;
        work.push_back(t);
      }
    }
  }
  cg.observable.erase(
      std::remove_if(cg.observable.begin(), cg.observable.end(),
                     [&](int obs) { return reached[static_cast<size_t>(obs)] != 0; }),
      cg.observable.end());
}

}  // namespace

CompiledSimGraph GraphCompiler::compile(const NormalizedGraph& ng) {
  CompiledSimGraph cg;
  std::map<int, int> y2c;
  std::vector<int> c2y;

  for (const auto& [id, net] : ng.nets) {
    (void)net;
    map_net(id, y2c, c2y);
  }

  std::vector<std::pair<int, const NormNode*>> ordered;
  for (const auto& [id, node] : ng.nodes) {
    ordered.emplace_back(id, &node);
  }
  std::sort(ordered.begin(), ordered.end(),
            [](const auto& a, const auto& b) {
              if (a.second->level != b.second->level) {
                return a.second->level < b.second->level;
              }
              return a.first < b.first;
            });

  std::vector<int> node_levels;
  std::vector<int> sim_owner_norm_id;
  for (const auto& [nid, node] : ordered) {
    (void)nid;
    if (node->type != NodeType::GATE && node->type != NodeType::CONST &&
        node->type != NodeType::FF && node->type != NodeType::LATCH) {
      continue;
    }

    if (node->type == NodeType::LATCH) {
      // An X-mode graph's latch (NormalizedGraph refuses one otherwise): a
      // stateful node in level order, as a clock gate is, its levels in
      // latch_configs. in0 D, in1 the enable, in2 clear, in3 preset.
      SimNode sn;
      sn.type = GateType::LATCH;
      sn.in0 = map_net(node->ff_config.data_net, y2c, c2y);
      sn.in1 = map_net(node->ff_config.clock_net, y2c, c2y);
      if (node->ff_config.clear.present) {
        sn.in2 = map_net(node->ff_config.clear.net, y2c, c2y);
      }
      if (node->ff_config.preset.present) {
        sn.in3 = map_net(node->ff_config.preset.net, y2c, c2y);
      }
      sn.out = map_net(node->ff_config.output_net, y2c, c2y);
      sn.ff_cfg = static_cast<uint32_t>(cg.latch_configs.size());
      CompiledFFConfig cfg;
      cfg.has_enable = true;
      cfg.enable_polarity = node->ff_config.latch_enable_level;
      cfg.has_clear = node->ff_config.clear.present;
      cfg.clear_polarity = node->ff_config.clear.polarity;
      cfg.clear_value = node->ff_config.clear.value;
      cfg.has_preset = node->ff_config.preset.present;
      cfg.preset_polarity = node->ff_config.preset.polarity;
      cfg.preset_value = node->ff_config.preset.value;
      cfg.clear_preset_conflict_value = node->ff_config.clear_preset_conflict_value;
      cg.latch_configs.push_back(cfg);
      node_levels.push_back(node->level);
      cg.nodes.push_back(sn);
      sim_owner_norm_id.push_back(nid);
      continue;
    }

    if (node->type == NodeType::FF) {
      SimNode sn;
      sn.type = GateType::DFF;
      sn.in0 = map_net(node->ff_config.data_net, y2c, c2y);
      sn.in1 = map_net(node->ff_config.clock_net, y2c, c2y);
      // clear and enable share the in2 slot: the current cell set never
      // combines them on one FF (edfxtp has no reset variant), so this is
      // safe today. Hard-fail rather than silently overwrite if that ever
      // changes -- a future combined cell needs its own dedicated slot, not
      // a silent collision here.
      if (node->ff_config.clear.present && node->ff_config.enable.present) {
        throw std::runtime_error(
            "FF cannot combine clear and enable controls on the same "
            "instance (normalized node " +
            std::to_string(nid) + "): unsupported cell");
      }
      if (node->ff_config.clear.present) {
        sn.in2 = map_net(node->ff_config.clear.net, y2c, c2y);
      } else if (node->ff_config.enable.present) {
        sn.in2 = map_net(node->ff_config.enable.net, y2c, c2y);
      }
      if (node->ff_config.preset.present) {
        sn.in3 = map_net(node->ff_config.preset.net, y2c, c2y);
      }
      if (node->ff_config.has_scan) {
        sn.in4 = map_net(node->ff_config.scan_in_net, y2c, c2y);
        sn.in5 = map_net(node->ff_config.scan_enable_net, y2c, c2y);
      }
      sn.out = map_net(node->ff_config.output_net, y2c, c2y);
      sn.ff_cfg = static_cast<uint32_t>(cg.ff_configs.size());

      CompiledFFConfig cfg;
      cfg.trigger = node->ff_config.trigger;
      cfg.has_clear = node->ff_config.clear.present;
      cfg.clear_polarity = node->ff_config.clear.polarity;
      cfg.clear_value = node->ff_config.clear.value;
      cfg.has_preset = node->ff_config.preset.present;
      cfg.preset_polarity = node->ff_config.preset.polarity;
      cfg.preset_value = node->ff_config.preset.value;
      cfg.has_scan = node->ff_config.has_scan;
      cfg.scan_enable_polarity = node->ff_config.scan_enable_polarity;
      cfg.has_enable = node->ff_config.enable.present;
      cfg.enable_polarity = node->ff_config.enable.polarity;
      cfg.clear_preset_conflict_value =
          node->ff_config.clear_preset_conflict_value;

      cg.ff_configs.push_back(cfg);
      node_levels.push_back(0);
      cg.nodes.push_back(sn);
      sim_owner_norm_id.push_back(nid);
      continue;
    }

    GateType gt = node->gate_type;
    if (gt == GateType::ADDF_S || gt == GateType::ADDH_S) {
      for (const auto& [pin, out_net] : node->output_pins) {
        SimNode sn;
        if (pin == "S" || pin == "YS") {
          sn.type =
              (gt == GateType::ADDF_S) ? GateType::ADDF_S : GateType::ADDH_S;
        } else {
          sn.type =
              (gt == GateType::ADDF_S) ? GateType::ADDF_CO : GateType::ADDH_CO;
        }
        wire_node_inputs(sn, sn.type, *node, y2c, c2y);
        sn.out = map_net(out_net, y2c, c2y);
        node_levels.push_back(node->level);
        cg.nodes.push_back(sn);
        sim_owner_norm_id.push_back(nid);
      }
      continue;
    }

    for (const auto& [pin, out_net] : node->output_pins) {
      (void)pin;
      SimNode sn;
      sn.type = gt;
      wire_node_inputs(sn, gt, *node, y2c, c2y);
      sn.out = map_net(out_net, y2c, c2y);
      node_levels.push_back(node->level);
      cg.nodes.push_back(sn);
      sim_owner_norm_id.push_back(nid);
    }
  }

  split_fanout_branches(cg, ng, sim_owner_norm_id, y2c, c2y, node_levels);
  // An X-mode graph may hold combinational loops (a two-valued one never does:
  // NormalizedGraph refuses them): level it by its strongly connected components.
  const bool cyclic = ng.x_mode && loop_levels_for(cg, c2y.size(), node_levels);
  rebuild_level_starts(cg, node_levels);
  if (cyclic) {
    flag_loops(cg, c2y.size());
  }
  cg.net_count = static_cast<int>(c2y.size());
  if (cg.net_sites.size() < static_cast<size_t>(cg.net_count)) {
    cg.net_sites.resize(static_cast<size_t>(cg.net_count));
  }
  cg.yosys_to_compiled = std::move(y2c);
  cg.compiled_to_yosys = std::move(c2y);
  cg.x_mode = ng.x_mode;

  for (int po : ng.POs) {
    if (cg.yosys_to_compiled.count(po) && !ng.unobserved.count(po)) {
      cg.observable.push_back(cg.yosys_to_compiled.at(po));
    }
  }
  // Phase 9: test points (e.g. blackbox input nets) become observable pseudo-POs.
  for (int tp : ng.TPs) {
    if (cg.yosys_to_compiled.count(tp) && !ng.unobserved.count(tp)) {
      const int cidx = cg.yosys_to_compiled.at(tp);
      if (std::find(cg.observable.begin(), cg.observable.end(), cidx) ==
          cg.observable.end()) {
        cg.observable.push_back(cidx);
      }
    }
  }
  for (int pi : ng.PIs) {
    if (cg.yosys_to_compiled.count(pi)) {
      cg.pi_nets.push_back(cg.yosys_to_compiled.at(pi));
    }
  }
  // Phase 9: controllable pseudo-PIs (blackbox output nets) are stimulus sources.
  for (int ppi : ng.pseudo_inputs) {
    if (cg.yosys_to_compiled.count(ppi)) {
      const int cidx = cg.yosys_to_compiled.at(ppi);
      if (std::find(cg.pi_nets.begin(), cg.pi_nets.end(), cidx) ==
          cg.pi_nets.end()) {
        cg.pi_nets.push_back(cidx);
        cg.pseudo_pi_nets.push_back(cidx);
      }
    }
  }

  rebuild_fanout_csr(cg);
  drop_unknown_reach(cg, ng);

  // driver_index: net -> node index that drives it (or -1). Topology-invariant,
  // so build it once here and reuse it in the ATPG cone extraction instead of
  // rebuilding it per fault.
  cg.driver_index.assign(static_cast<size_t>(cg.net_count), -1);
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const uint32_t out = cg.nodes[i].out;
    if (out < static_cast<uint32_t>(cg.net_count)) {
      cg.driver_index[out] = static_cast<int>(i);
    }
  }
  return cg;
}

std::string canonical_site_key(const CompiledSimGraph& cg, uint32_t cidx) {
  if (cidx >= static_cast<uint32_t>(cg.net_count)) {
    throw std::runtime_error("compiled net index out of range");
  }
  const int yid = cg.compiled_to_yosys[cidx];
  if (cidx < cg.net_sites.size()) {
    const NetSiteInfo& site = cg.net_sites[cidx];
    if (site.kind == SiteKind::BRANCH) {
      if (site.consumer_instance.empty() || site.input_pin.empty()) {
        throw std::runtime_error("branch site missing consumer or pin");
      }
      return "net:" + std::to_string(yid) + ":branch:" + site.consumer_instance +
             ":" + site.input_pin;
    }
  }
  return "net:" + std::to_string(yid) + ":stem";
}

}  // namespace faultflow
