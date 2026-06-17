#include "fault/collapser/fault_collapser.hpp"

#include <cstdint>
#include <limits>
#include <map>
#include <utility>

namespace faultflow {
namespace {

using FaultKey = std::pair<uint32_t, FaultType>;

constexpr uint32_t kNoFault = std::numeric_limits<uint32_t>::max();

uint32_t find_fault(const std::map<FaultKey, uint32_t>& index, uint32_t net,
                    FaultType type) {
  const auto it = index.find({net, type});
  return it == index.end() ? kNoFault : it->second;
}

// Collapse fault `from` into representative `to` (sets collapsed_into). Never
// collapse a fault that is excluded, nor into an excluded representative, nor a
// fault into itself.
void collapse_to(std::vector<CompactFault>& faults, uint32_t from, uint32_t to) {
  if (from == kNoFault || to == kNoFault || from == to) {
    return;
  }
  if (faults[from].exclusion != FaultExclusion::NONE ||
      faults[to].exclusion != FaultExclusion::NONE) {
    return;
  }
  faults[from].collapsed_into = to;
}

// Equivalence-only collapse spec for a primitive gate: which input-fault
// polarity is equivalent to which output-fault polarity. For AND/OR/NAND/NOR
// only the controlling-value input polarity is a true equivalence (the other
// polarity is a dominance relation, deliberately NOT collapsed). INV/BUF are
// bijective and collapse both polarities.
struct EquivRule {
  bool collapse_sa0 = false;
  FaultType sa0_target = FaultType::SA0;
  bool collapse_sa1 = false;
  FaultType sa1_target = FaultType::SA1;
};

bool equiv_rule_for(GateType type, EquivRule& rule) {
  switch (type) {
    case GateType::INV:  // in/0 == out/1 ; in/1 == out/0
      rule = {true, FaultType::SA1, true, FaultType::SA0};
      return true;
    case GateType::BUF:  // in/0 == out/0 ; in/1 == out/1
      rule = {true, FaultType::SA0, true, FaultType::SA1};
      return true;
    case GateType::AND2:
    case GateType::AND3:
    case GateType::AND4:  // in/0 == out/0
      rule = {true, FaultType::SA0, false, FaultType::SA1};
      return true;
    case GateType::OR2:
    case GateType::OR3:
    case GateType::OR4:  // in/1 == out/1
      rule = {false, FaultType::SA0, true, FaultType::SA1};
      return true;
    case GateType::NAND2:
    case GateType::NAND3:
    case GateType::NAND4:  // in/0 == out/1
      rule = {true, FaultType::SA1, false, FaultType::SA1};
      return true;
    case GateType::NOR2:
    case GateType::NOR3:
    case GateType::NOR4:  // in/1 == out/0
      rule = {false, FaultType::SA0, true, FaultType::SA0};
      return true;
    default:
      // XOR/XNOR (no controlling value), bubbled-input "B" variants, compound
      // AOI/OAI/A*/O* cells, MUX, ADDF/ADDH, CONST, INPUT (PI source) and FF are
      // left uncollapsed: their faults are not simple structural equivalents.
      return false;
  }
}

}  // namespace

std::vector<CompactFault> collapse_primitive_faults(
    const NormalizedGraph& /*ng*/, const CompiledSimGraph& cg,
    std::vector<CompactFault> faults) {
  // Faults are enumerated SA0 then SA1 per compiled net, but build an explicit
  // (net, type) -> index map so we never rely on that ordering.
  std::map<FaultKey, uint32_t> index;
  for (uint32_t i = 0; i < faults.size(); ++i) {
    index[{faults[i].net_index, faults[i].type}] = i;
  }

  // Fanout of a compiled net from the post-split CSR. Only a fanout-free net
  // (exactly one consumer) is a valid equivalence source: it guarantees the
  // input fault can propagate ONLY through this gate, and it automatically
  // skips the fanout-split BUF stems (whose stem input has fanout > 1).
  const auto fanout_of = [&](uint32_t net) -> uint32_t {
    if (static_cast<size_t>(net) + 1 < cg.fanout_offsets.size()) {
      return cg.fanout_offsets[net + 1] - cg.fanout_offsets[net];
    }
    return 0;
  };

  for (const SimNode& node : cg.nodes) {
    EquivRule rule;
    if (!equiv_rule_for(node.type, rule)) {
      continue;
    }
    const uint32_t out = node.out;
    const uint32_t inputs[] = {node.in0, node.in1, node.in2,
                               node.in3, node.in4, node.in5};
    for (uint32_t in : inputs) {
      if (in == UNUSED_INPUT || fanout_of(in) != 1) {
        continue;
      }
      if (rule.collapse_sa0) {
        collapse_to(faults, find_fault(index, in, FaultType::SA0),
                    find_fault(index, out, rule.sa0_target));
      }
      if (rule.collapse_sa1) {
        collapse_to(faults, find_fault(index, in, FaultType::SA1),
                    find_fault(index, out, rule.sa1_target));
      }
    }
  }
  return faults;
}

}  // namespace faultflow
