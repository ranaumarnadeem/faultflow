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
      wire("A", sn.in0);
      wire("B", sn.in1);
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
      break;
    case GateType::ADDF_S:
    case GateType::ADDF_CO:
      wire("A", sn.in0);
      wire("B", sn.in1);
      wire("C", sn.in2);
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

void split_fanout_branches(CompiledSimGraph& cg, std::map<int, int>& y2c,
                           std::vector<int>& c2y, std::vector<int>& node_levels) {
  const uint32_t net_count = static_cast<uint32_t>(c2y.size());
  std::vector<std::vector<FanoutEdge>> fanout_edges(net_count);

  for (size_t ni = 0; ni < cg.nodes.size(); ++ni) {
    SimNode& sn = cg.nodes[ni];
    for (int slot = 0; slot < 6; ++slot) {
      uint32_t* in = input_slot(sn, slot);
      if (in != nullptr && *in != UNUSED_INPUT) {
        fanout_edges[*in].push_back({ni, slot});
      }
    }
  }

  for (uint32_t stem = 0; stem < net_count; ++stem) {
    if (fanout_edges[stem].size() <= 1) {
      continue;
    }
    for (const FanoutEdge& edge : fanout_edges[stem]) {
      const int consumer_level = static_cast<int>(node_levels[edge.node_idx]);
      const int source_yosys_id = c2y.at(stem);
      const uint32_t branch = append_branch_alias(c2y, source_yosys_id);
      SimNode buf;
      buf.type = GateType::BUF;
      buf.in0 = stem;
      buf.out = branch;
      node_levels.push_back(std::max(0, consumer_level - 1));
      cg.nodes.push_back(buf);

      uint32_t* target = input_slot(cg.nodes[edge.node_idx], edge.slot);
      if (target != nullptr) {
        *target = branch;
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
  for (const auto& [nid, node] : ordered) {
    (void)nid;
    if (node->type != NodeType::GATE && node->type != NodeType::CONST &&
        node->type != NodeType::FF) {
      continue;
    }

    if (node->type == NodeType::FF) {
      SimNode sn;
      sn.type = GateType::DFF;
      sn.in0 = map_net(node->ff_config.data_net, y2c, c2y);
      sn.in1 = map_net(node->ff_config.clock_net, y2c, c2y);
      if (node->ff_config.clear.present) {
        sn.in2 = map_net(node->ff_config.clear.net, y2c, c2y);
      }
      if (node->ff_config.preset.present) {
        sn.in3 = map_net(node->ff_config.preset.net, y2c, c2y);
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
      cfg.clear_preset_conflict_value =
          node->ff_config.clear_preset_conflict_value;

      cg.ff_configs.push_back(cfg);
      node_levels.push_back(0);
      cg.nodes.push_back(sn);
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
        wire_inputs(sn, sn.type, node->input_pins, y2c, c2y);
        sn.out = map_net(out_net, y2c, c2y);
        node_levels.push_back(node->level);
        cg.nodes.push_back(sn);
      }
      continue;
    }

    for (const auto& [pin, out_net] : node->output_pins) {
      (void)pin;
      SimNode sn;
      sn.type = gt;
      wire_inputs(sn, gt, node->input_pins, y2c, c2y);
      sn.out = map_net(out_net, y2c, c2y);
      node_levels.push_back(node->level);
      cg.nodes.push_back(sn);
    }
  }

  split_fanout_branches(cg, y2c, c2y, node_levels);
  rebuild_level_starts(cg, node_levels);
  cg.net_count = static_cast<int>(c2y.size());
  cg.yosys_to_compiled = std::move(y2c);
  cg.compiled_to_yosys = std::move(c2y);

  for (int po : ng.POs) {
    if (cg.yosys_to_compiled.count(po)) {
      cg.observable.push_back(cg.yosys_to_compiled.at(po));
    }
  }
  for (int pi : ng.PIs) {
    if (cg.yosys_to_compiled.count(pi)) {
      cg.pi_nets.push_back(cg.yosys_to_compiled.at(pi));
    }
  }

  rebuild_fanout_csr(cg);
  return cg;
}

}  // namespace faultflow
