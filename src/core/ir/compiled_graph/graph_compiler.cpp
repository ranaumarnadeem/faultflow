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

}  // namespace

CompiledSimGraph GraphCompiler::compile(const NormalizedGraph& ng) {
  CompiledSimGraph cg;
  std::map<int, int> y2c;
  std::vector<int> c2y;

  for (const auto& [id, net] : ng.nets) {
    if (!net.is_blackboxed) {
      map_net(id, y2c, c2y);
    }
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

  std::vector<int> levels;
  for (const auto& [nid, node] : ordered) {
    (void)nid;
    if (node->type != NodeType::GATE && node->type != NodeType::CONST) {
      continue;
    }

    GateType gt = node->gate_type;
    if (gt == GateType::ADDF_S || gt == GateType::ADDH_S) {
      // Multi-output lowering
      for (const auto& [pin, out_net] : node->output_pins) {
        (void)pin;
        SimNode sn;
        if (pin == "S" || pin == "YS") {
          sn.type = (gt == GateType::ADDF_S) ? GateType::ADDF_S : GateType::ADDH_S;
        } else {
          sn.type = (gt == GateType::ADDF_S) ? GateType::ADDF_CO : GateType::ADDH_CO;
        }
        wire_inputs(sn, sn.type, node->input_pins, y2c, c2y);
        sn.out = map_net(out_net, y2c, c2y);
        levels.push_back(node->level);
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
      levels.push_back(node->level);
      cg.nodes.push_back(sn);
    }
  }

  if (!cg.nodes.empty()) {
    int cur = levels[0];
    cg.level_starts.push_back(0);
    for (size_t i = 1; i < cg.nodes.size(); ++i) {
      if (levels[i] != cur) {
        cg.level_starts.push_back(static_cast<int>(i));
        cur = levels[i];
      }
    }
    cg.level_starts.push_back(static_cast<int>(cg.nodes.size()));
  }

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

  std::vector<std::vector<uint32_t>> fanout_lists(cg.net_count);
  for (const auto& sn : cg.nodes) {
    const uint32_t ins[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5};
    for (uint32_t in : ins) {
      if (in != UNUSED_INPUT) {
        fanout_lists[in].push_back(sn.out);
      }
    }
  }
  cg.fanout_offsets.push_back(0);
  for (const auto& fo : fanout_lists) {
    cg.fanout_targets.insert(cg.fanout_targets.end(), fo.begin(), fo.end());
    cg.fanout_offsets.push_back(
        static_cast<uint32_t>(cg.fanout_targets.size()));
  }

  return cg;
}

}  // namespace faultflow
