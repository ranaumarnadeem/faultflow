#include "sim/golden_ref/golden_ref_sim.hpp"

#include <algorithm>

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

bool fault_value(const CompactFault& fault) {
  return fault.type != FaultType::SA0;
}

bool has_fault(const CompactFault* fault) {
  return fault != nullptr && fault->exclusion == FaultExclusion::NONE;
}

void apply_fault(std::vector<bool>& values, const CompactFault* fault,
                 uint32_t net) {
  if (has_fault(fault) && fault->net_index == net) {
    values[net] = fault_value(*fault);
  }
}

std::vector<TestCycle> cycles_for(const TestVector& vec) {
  if (!vec.cycles.empty()) {
    return vec.cycles;
  }
  TestCycle c;
  c.inputs = vec.inputs;
  c.sample_outputs = true;
  return {c};
}

void broadcast_inputs(const CompiledSimGraph& cg, const TestCycle& cycle,
                      std::vector<bool>& values, const CompactFault* fault) {
  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    auto it = cycle.inputs.find(yid);
    values[pi_idx] = (it != cycle.inputs.end()) ? it->second : false;
    apply_fault(values, fault, static_cast<uint32_t>(pi_idx));
  }
}

std::map<int, bool> snapshot(const CompiledSimGraph& cg,
                             const std::vector<bool>& values) {
  std::map<int, bool> out;
  for (size_t i = 0; i < cg.compiled_to_yosys.size(); ++i) {
    out[cg.compiled_to_yosys[i]] = values[i];
  }
  return out;
}

void seed_ff_outputs(const CompiledSimGraph& cg,
                     const std::vector<bool>& ff_states,
                     std::vector<bool>& values, const CompactFault* fault) {
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    values[sn.out] = ff_states.at(idx);
    apply_fault(values, fault, sn.out);
  }
}

void evaluate_combinational(const CompiledSimGraph& cg,
                            std::vector<bool>& values,
                            const CompactFault* fault) {
  const auto eval_node = [&](const SimNode& sn) {
    if (sn.type == GateType::INPUT || sn.type == GateType::DFF) {
      return;
    }
    if (sn.type == GateType::CONST0) {
      values[sn.out] = false;
      apply_fault(values, fault, sn.out);
      return;
    }
    if (sn.type == GateType::CONST1) {
      values[sn.out] = true;
      apply_fault(values, fault, sn.out);
      return;
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
    apply_fault(values, fault, sn.out);
  };

  if (cg.level_starts.size() >= 2) {
    for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
      for (int i = cg.level_starts[lvl]; i < cg.level_starts[lvl + 1]; ++i) {
        eval_node(cg.nodes[i]);
      }
    }
    return;
  }
  for (const auto& sn : cg.nodes) {
    eval_node(sn);
  }
}

bool control_active(bool value, Polarity polarity) {
  return polarity == Polarity::ACTIVE_HIGH ? value : !value;
}

bool edge_active(bool prev, bool cur, TriggerType trigger) {
  if (trigger == TriggerType::POSEDGE) {
    return !prev && cur;
  }
  return prev && !cur;
}

void update_ff_states(const CompiledSimGraph& cg,
                      const std::vector<bool>& values,
                      const std::vector<bool>& prev_values,
                      std::vector<bool>& ff_states) {
  std::vector<bool> next = ff_states;
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    const CompiledFFConfig& cfg = cg.ff_configs.at(sn.ff_cfg);
    const bool clear_active =
        cfg.has_clear && sn.in2 != UNUSED_INPUT &&
        control_active(values[sn.in2], cfg.clear_polarity);
    const bool preset_active =
        cfg.has_preset && sn.in3 != UNUSED_INPUT &&
        control_active(values[sn.in3], cfg.preset_polarity);
    if (clear_active && preset_active) {
      next[idx] = cfg.clear_preset_conflict_value != 0;
      continue;
    }
    if (clear_active) {
      next[idx] = cfg.clear_value != 0;
      continue;
    }
    if (preset_active) {
      next[idx] = cfg.preset_value != 0;
      continue;
    }
    if (edge_active(prev_values[sn.in1], values[sn.in1], cfg.trigger)) {
      next[idx] = values[sn.in0];
    }
  }
  ff_states = std::move(next);
}

}  // namespace

std::map<int, bool> GoldenRefSim::simulate_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec) const {
  if (!cg.ff_configs.empty() || vec.is_sequential()) {
    const auto samples = simulate_sequence_fault_free(cg, vec);
    if (!samples.empty()) {
      return samples.back();
    }
  }
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
          if (sn.type == GateType::INPUT || sn.type == GateType::DFF) {
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
      if (sn.type == GateType::DFF) {
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
  if (!cg.ff_configs.empty() || vec.is_sequential()) {
    const auto samples = simulate_sequence_with_fault(cg, vec, fault);
    if (!samples.empty()) {
      return samples.back();
    }
  }
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
          if (sn.type == GateType::INPUT || sn.type == GateType::DFF) {
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
      if (sn.type == GateType::DFF) {
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

std::vector<std::map<int, bool>> GoldenRefSim::simulate_sequence_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec) const {
  CompactFault none;
  none.exclusion = FaultExclusion::BLACKBOX;
  return simulate_sequence_with_fault(cg, vec, none);
}

std::vector<std::map<int, bool>> GoldenRefSim::simulate_sequence_with_fault(
    const CompiledSimGraph& cg, const TestVector& vec,
    const CompactFault& fault) const {
  std::vector<bool> values(cg.net_count, false);
  std::vector<bool> prev_values(cg.net_count, false);
  std::vector<bool> ff_states(cg.ff_configs.size(), false);
  for (const auto& [idx, value] : vec.initial_ff_state) {
    if (idx < ff_states.size()) {
      ff_states[idx] = value;
    }
  }
  const CompactFault* active_fault =
      fault.exclusion == FaultExclusion::NONE ? &fault : nullptr;
  std::vector<std::map<int, bool>> samples;

  for (const TestCycle& cycle : cycles_for(vec)) {
    broadcast_inputs(cg, cycle, values, active_fault);
    seed_ff_outputs(cg, ff_states, values, active_fault);
    evaluate_combinational(cg, values, active_fault);
    update_ff_states(cg, values, prev_values, ff_states);
    seed_ff_outputs(cg, ff_states, values, active_fault);
    evaluate_combinational(cg, values, active_fault);
    for (int i = 0; i < cycle.settle_cycles; ++i) {
      seed_ff_outputs(cg, ff_states, values, active_fault);
      evaluate_combinational(cg, values, active_fault);
    }
    if (cycle.sample_outputs) {
      samples.push_back(snapshot(cg, values));
    }
    for (size_t i = 0; i < values.size(); ++i) {
      prev_values[i] = values[i];
    }
  }
  return samples;
}

bool GoldenRefSim::is_sequence_detected(
    const CompiledSimGraph& cg,
    const std::vector<std::map<int, bool>>& fault_free,
    const std::vector<std::map<int, bool>>& faulty) const {
  const size_t count = std::min(fault_free.size(), faulty.size());
  for (size_t i = 0; i < count; ++i) {
    if (is_detected(cg, fault_free[i], faulty[i])) {
      return true;
    }
  }
  return false;
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
