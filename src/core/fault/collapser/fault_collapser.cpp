#include "fault/collapser/fault_collapser.hpp"

#include <limits>
#include <map>

namespace faultflow {
namespace {

using FaultKey = std::pair<uint32_t, FaultType>;

uint32_t find_fault(const std::map<FaultKey, uint32_t>& index, uint32_t net,
                    FaultType type) {
  const auto it = index.find({net, type});
  if (it == index.end()) {
    return std::numeric_limits<uint32_t>::max();
  }
  return it->second;
}

void collapse_to(std::vector<CompactFault>& faults, uint32_t from, uint32_t to) {
  if (from == std::numeric_limits<uint32_t>::max() ||
      to == std::numeric_limits<uint32_t>::max() || from == to) {
    return;
  }
  if (faults[from].exclusion != FaultExclusion::NONE ||
      faults[to].exclusion != FaultExclusion::NONE) {
    return;
  }
  faults[from].collapsed_into = to;
}

}  // namespace

std::vector<CompactFault> collapse_primitive_faults(
    const NormalizedGraph& ng, const CompiledSimGraph& cg,
    std::vector<CompactFault> faults) {
  std::map<FaultKey, uint32_t> index;
  for (uint32_t i = 0; i < faults.size(); ++i) {
    index[{faults[i].net_index, faults[i].type}] = i;
  }

  for (const auto& [_, node] : ng.nodes) {
    if (node.output_pins.empty()) {
      continue;
    }
    const int out_yid = node.output_pins.begin()->second;
    const auto out_it = cg.yosys_to_compiled.find(out_yid);
    if (out_it == cg.yosys_to_compiled.end()) {
      continue;
    }
    const uint32_t out = static_cast<uint32_t>(out_it->second);

    const auto collapse_inputs_to = [&](FaultType out_type) {
      const uint32_t target = find_fault(index, out, out_type);
      for (const auto& [__, in_yid] : node.input_pins) {
        const auto in_it = cg.yosys_to_compiled.find(in_yid);
        if (in_it == cg.yosys_to_compiled.end()) {
          continue;
        }
        const uint32_t in = static_cast<uint32_t>(in_it->second);
        collapse_to(faults, find_fault(index, in, FaultType::SA0), target);
        collapse_to(faults, find_fault(index, in, FaultType::SA1), target);
      }
    };

    if (node.gate_type == GateType::INV || node.gate_type == GateType::BUF) {
      for (const auto& [__, in_yid] : node.input_pins) {
        const auto in_it = cg.yosys_to_compiled.find(in_yid);
        if (in_it == cg.yosys_to_compiled.end()) {
          continue;
        }
        const uint32_t in = static_cast<uint32_t>(in_it->second);
        if (node.gate_type == GateType::INV) {
          collapse_to(faults, find_fault(index, in, FaultType::SA0),
                      find_fault(index, out, FaultType::SA1));
          collapse_to(faults, find_fault(index, in, FaultType::SA1),
                      find_fault(index, out, FaultType::SA0));
        } else {
          collapse_to(faults, find_fault(index, in, FaultType::SA0),
                      find_fault(index, out, FaultType::SA0));
          collapse_to(faults, find_fault(index, in, FaultType::SA1),
                      find_fault(index, out, FaultType::SA1));
        }
      }
    } else if (node.gate_type == GateType::AND2) {
      collapse_inputs_to(FaultType::SA0);
    } else if (node.gate_type == GateType::OR2) {
      collapse_inputs_to(FaultType::SA1);
    } else if (node.gate_type == GateType::NAND2) {
      collapse_inputs_to(FaultType::SA1);
    } else if (node.gate_type == GateType::NOR2) {
      collapse_inputs_to(FaultType::SA0);
    }
  }

  return faults;
}

}  // namespace faultflow
