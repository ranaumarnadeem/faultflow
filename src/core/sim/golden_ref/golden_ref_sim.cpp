#include "sim/golden_ref/golden_ref_sim.hpp"

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

bool fault_value(const CompactFault& fault) {
  return fault.type != FaultType::SA0;
}

}  // namespace

std::map<int, bool> GoldenRefSim::simulate_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec) const {
  std::vector<bool> values(cg.net_count, false);

  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    auto it = vec.inputs.find(yid);
    values[pi_idx] = (it != vec.inputs.end()) ? it->second : false;
  }

  const auto run_levels = [&]() {
    if (cg.level_starts.size() >= 2) {
      for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
        const int start = cg.level_starts[lvl];
        const int end = cg.level_starts[lvl + 1];
        for (int i = start; i < end; ++i) {
          const SimNode& sn = cg.nodes[i];
          if (sn.type == GateType::INPUT) {
            continue;
          }
          if (sn.type == GateType::CONST0) {
            values[sn.out] = false;
            continue;
          }
          if (sn.type == GateType::CONST1) {
            values[sn.out] = true;
            continue;
          }
          std::vector<bool> ins;
          const uint32_t in_slots[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4,
                                       sn.in5};
          for (uint32_t slot : in_slots) {
            if (slot != UNUSED_INPUT) {
              ins.push_back(values[slot]);
            }
          }
          values[sn.out] = eval_gate_scalar(sn.type, ins);
        }
      }
      return;
    }
    for (const auto& sn : cg.nodes) {
      if (sn.type == GateType::INPUT) {
        continue;
      }
      if (sn.type == GateType::CONST0) {
        values[sn.out] = false;
        continue;
      }
      if (sn.type == GateType::CONST1) {
        values[sn.out] = true;
        continue;
      }
      std::vector<bool> ins;
      const uint32_t in_slots[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4,
                                   sn.in5};
      for (uint32_t slot : in_slots) {
        if (slot != UNUSED_INPUT) {
          ins.push_back(values[slot]);
        }
      }
      values[sn.out] = eval_gate_scalar(sn.type, ins);
    }
  };

  run_levels();

  std::map<int, bool> out;
  for (size_t i = 0; i < cg.compiled_to_yosys.size(); ++i) {
    out[cg.compiled_to_yosys[i]] = values[i];
  }
  return out;
}

std::map<int, bool> GoldenRefSim::simulate_with_fault(
    const CompiledSimGraph& cg, const TestVector& vec,
    const CompactFault& fault) const {
  if (fault.exclusion != FaultExclusion::NONE) {
    return simulate_fault_free(cg, vec);
  }

  std::vector<bool> values(cg.net_count, false);

  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    auto it = vec.inputs.find(yid);
    values[pi_idx] = (it != vec.inputs.end()) ? it->second : false;
    if (fault.net_index == static_cast<uint32_t>(pi_idx)) {
      values[pi_idx] = fault_value(fault);
    }
  }

  const auto run_levels = [&]() {
    if (cg.level_starts.size() >= 2) {
      for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
        const int start = cg.level_starts[lvl];
        const int end = cg.level_starts[lvl + 1];
        for (int i = start; i < end; ++i) {
          const SimNode& sn = cg.nodes[i];
          if (sn.type == GateType::INPUT) {
            continue;
          }
          if (sn.type == GateType::CONST0) {
            values[sn.out] = false;
          } else if (sn.type == GateType::CONST1) {
            values[sn.out] = true;
          } else {
            std::vector<bool> ins;
            const uint32_t in_slots[] = {sn.in0, sn.in1, sn.in2, sn.in3,
                                           sn.in4, sn.in5};
            for (uint32_t slot : in_slots) {
              if (slot != UNUSED_INPUT) {
                ins.push_back(values[slot]);
              }
            }
            values[sn.out] = eval_gate_scalar(sn.type, ins);
          }
          if (fault.net_index == sn.out) {
            values[sn.out] = fault_value(fault);
          }
        }
      }
      return;
    }
    for (const auto& sn : cg.nodes) {
      if (sn.type == GateType::INPUT) {
        continue;
      }
      if (sn.type == GateType::CONST0) {
        values[sn.out] = false;
      } else if (sn.type == GateType::CONST1) {
        values[sn.out] = true;
      } else {
        std::vector<bool> ins;
        const uint32_t in_slots[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4,
                                     sn.in5};
        for (uint32_t slot : in_slots) {
          if (slot != UNUSED_INPUT) {
            ins.push_back(values[slot]);
          }
        }
        values[sn.out] = eval_gate_scalar(sn.type, ins);
      }
      if (fault.net_index == sn.out) {
        values[sn.out] = fault_value(fault);
      }
    }
  };

  run_levels();

  std::map<int, bool> out;
  for (size_t i = 0; i < cg.compiled_to_yosys.size(); ++i) {
    out[cg.compiled_to_yosys[i]] = values[i];
  }
  return out;
}

bool GoldenRefSim::is_detected(const CompiledSimGraph& cg,
                               const std::map<int, bool>& fault_free,
                               const std::map<int, bool>& faulty) const {
  for (int cidx : cg.observable) {
    const int yid = cg.compiled_to_yosys[cidx];
    if (fault_free.at(yid) != faulty.at(yid)) {
      return true;
    }
  }
  return false;
}

}  // namespace faultflow
