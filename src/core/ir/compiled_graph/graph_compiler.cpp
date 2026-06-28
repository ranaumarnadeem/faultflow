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
    // IEEE 1500 wrapper cells: the functional data input feeds in0 (the WBR
    // node drives its stable output net; FUNCTIONAL = buffer of in0). For the
    // shiftable scan variant the mode-mux also reads the FF state q via the CTO
    // pin -> in1 (build_mode_config selects in0=CFI vs in1=q per mode); the
    // transparent buffer cells have no CTO pin, so in1 stays UNUSED for them.
    case GateType::WBR_IN:
      wire("FROM_SYS", sn.in0);
      wire("CTO", sn.in1);
      break;
    case GateType::WBR_OUT:
      wire("FROM_CORE", sn.in0);
      wire("CTO", sn.in1);
      break;
    case GateType::INPUT:
    case GateType::DFF:
      break;
    case GateType::AND2:
    case GateType::AND2B:
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
      wire_seq({"A1", "A2", "A3", "B1", "C1"});
      break;
    case GateType::A222OI:
      wire_seq({"A1", "A2", "B1", "B2", "C1", "C2"});
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
      wire("A_N", sn.in0);
      wire("B_N", sn.in1);
      wire("C", sn.in2);
      wire("D", sn.in3);
      break;
    case GateType::O2111A:
    case GateType::O2111AI:
      wire_seq({"A1", "A2", "B1", "C1", "D1"});
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
          input_pin = pin_for_compiled_input(norm, y2c, stem);
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
      node_levels.push_back(std::max(0, consumer_level - 1));
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
  std::vector<int> sim_owner_norm_id;
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
        wire_inputs(sn, sn.type, node->input_pins, y2c, c2y);
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
      wire_inputs(sn, gt, node->input_pins, y2c, c2y);
      sn.out = map_net(out_net, y2c, c2y);
      node_levels.push_back(node->level);
      cg.nodes.push_back(sn);
      sim_owner_norm_id.push_back(nid);
    }
  }

  split_fanout_branches(cg, ng, sim_owner_norm_id, y2c, c2y, node_levels);
  rebuild_level_starts(cg, node_levels);
  cg.net_count = static_cast<int>(c2y.size());
  if (cg.net_sites.size() < static_cast<size_t>(cg.net_count)) {
    cg.net_sites.resize(static_cast<size_t>(cg.net_count));
  }
  cg.yosys_to_compiled = std::move(y2c);
  cg.compiled_to_yosys = std::move(c2y);

  for (int po : ng.POs) {
    if (cg.yosys_to_compiled.count(po)) {
      cg.observable.push_back(cg.yosys_to_compiled.at(po));
    }
  }
  // Phase 9: test points (e.g. blackbox input nets) become observable pseudo-POs.
  for (int tp : ng.TPs) {
    if (cg.yosys_to_compiled.count(tp)) {
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

  // IEEE 1500 wrapper boundary cells -> compiled-index (stem) space. Each WBR
  // node already drives the cell's stem output net; we only record the
  // core/sys index pair so build_mode_config can reconfigure points per mode.
  for (const auto& wc : ng.wrapper_cells) {
    auto cit = cg.yosys_to_compiled.find(wc.core_net);
    auto sit = cg.yosys_to_compiled.find(wc.sys_net);
    if (cit == cg.yosys_to_compiled.end() ||
        sit == cg.yosys_to_compiled.end()) {
      continue;
    }
    CompiledWrapperCell cwc;
    cwc.core_idx = static_cast<uint32_t>(cit->second);
    cwc.sys_idx = static_cast<uint32_t>(sit->second);
    cwc.is_input = wc.is_input;
    if (wc.scan) {
      cwc.scan = true;
      auto qit = cg.yosys_to_compiled.find(wc.cto_net);
      if (qit != cg.yosys_to_compiled.end()) {
        cwc.cto_idx = static_cast<uint32_t>(qit->second);
      }
    }
    cg.wrapper_cells.push_back(cwc);
  }

  rebuild_fanout_csr(cg);
  return cg;
}

ModeConfig build_mode_config(const CompiledSimGraph& cg, TestMode mode) {
  ModeConfig mc;
  mc.mode = mode;
  mc.wbr_action.assign(static_cast<size_t>(cg.net_count),
                       static_cast<uint8_t>(WbrAction::PASS));

  // FUNCTIONAL: wrapper cells are transparent buffers; point sets unchanged.
  if (mode == TestMode::FUNCTIONAL) {
    mc.stimulus_nets.assign(cg.pi_nets.begin(), cg.pi_nets.end());
    mc.observable_nets.assign(cg.observable.begin(), cg.observable.end());
    return mc;
  }

  auto push_unique = [](std::vector<uint32_t>& v, uint32_t x) {
    if (std::find(v.begin(), v.end(), x) == v.end()) {
      v.push_back(x);
    }
  };
  const auto set = [](WbrAction a) { return static_cast<uint8_t>(a); };

  // Each wrapper cell contributes a control point OR an observe point depending
  // on mode (IEEE 1500 boundary truth table). The driven net (core for WBR_IN,
  // sys for WBR_OUT) is either a stimulus (skip, retain broadcast) or forced 0.
  for (const auto& wc : cg.wrapper_cells) {
    // Native shiftable WBR: the FF state q drives the active functional output
    // (INTEST: WBR_IN -> core; EXTEST: WBR_OUT -> interconnect); the inactive
    // side is held safe-0. Control + observe happen through the wrapper scan
    // chain (load q / unload q), NOT via broadcast stimulus or combinational
    // observe-point sets, so only the per-net mode-mux action is set here.
    if (wc.scan) {
      const bool active =
          (mode == TestMode::INTEST) ? wc.is_input : !wc.is_input;
      const uint32_t drive = wc.is_input ? wc.core_idx : wc.sys_idx;
      if (active) {
        mc.wbr_action[drive] = set(WbrAction::DRIVE_FROM_FF);
      } else {
        push_unique(mc.safe_zero_nets, drive);
        mc.wbr_action[drive] = set(WbrAction::FORCE_ZERO);
      }
      continue;
    }
    if (mode == TestMode::INTEST) {
      if (wc.is_input) {  // drive core inputs (control)
        push_unique(mc.stimulus_nets, wc.core_idx);
        mc.wbr_action[wc.core_idx] = set(WbrAction::SKIP_STIMULUS);
      } else {  // observe core outputs; drive sys side safe-0
        push_unique(mc.observable_nets, wc.core_idx);
        push_unique(mc.safe_zero_nets, wc.sys_idx);
        mc.wbr_action[wc.sys_idx] = set(WbrAction::FORCE_ZERO);
      }
    } else {  // EXTEST
      if (wc.is_input) {  // observe interconnect; drive core side safe-0
        push_unique(mc.observable_nets, wc.sys_idx);
        push_unique(mc.safe_zero_nets, wc.core_idx);
        mc.wbr_action[wc.core_idx] = set(WbrAction::FORCE_ZERO);
      } else {  // drive interconnect (control)
        push_unique(mc.stimulus_nets, wc.sys_idx);
        mc.wbr_action[wc.sys_idx] = set(WbrAction::SKIP_STIMULUS);
      }
    }
  }

  if (mode == TestMode::EXTEST) {
    // Top PIs also drive the interconnect; base POs not used as a control
    // point remain observable interconnect endpoints.
    for (int pi : cg.pi_nets) {
      push_unique(mc.stimulus_nets, static_cast<uint32_t>(pi));
    }
    for (int po : cg.observable) {
      const uint32_t cidx = static_cast<uint32_t>(po);
      if (std::find(mc.stimulus_nets.begin(), mc.stimulus_nets.end(), cidx) ==
          mc.stimulus_nets.end()) {
        push_unique(mc.observable_nets, cidx);
      }
    }
  }

  return mc;
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
