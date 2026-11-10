#include "sim/xsim/golden_ref_sim_x.hpp"

#include <algorithm>

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

void apply_fault(std::vector<Logic>& values, const CompactFault* fault, uint32_t net) {
  if (fault != nullptr && fault->net_index == net) {
    values[net] = logic_of(fault->type != FaultType::SA0);
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
                      const std::set<int>* unknown, std::vector<Logic>& values,
                      const CompactFault* fault) {
  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    if (unknown != nullptr && unknown->count(yid) != 0) {
      values[pi_idx] = Logic::X;
    } else {
      auto it = cycle.inputs.find(yid);
      values[pi_idx] = logic_of(it != cycle.inputs.end() && it->second);
    }
    apply_fault(values, fault, static_cast<uint32_t>(pi_idx));
  }
}

XSnapshot snapshot(const CompiledSimGraph& cg, const std::vector<Logic>& values,
                   const std::vector<int>* sample_yids) {
  XSnapshot out;
  if (sample_yids == nullptr) {
    // The stem's value per Yosys id, as GoldenRefSim's snapshot (not a branch's).
    for (const auto& [yid, cidx] : cg.yosys_to_compiled) {
      out[yid] = values[static_cast<size_t>(cidx)];
    }
    return out;
  }
  for (int yid : *sample_yids) {
    auto it = cg.yosys_to_compiled.find(yid);
    if (it != cg.yosys_to_compiled.end()) {
      out[yid] = values[static_cast<size_t>(it->second)];
    }
  }
  return out;
}

void seed_ff_outputs(const CompiledSimGraph& cg, const std::vector<Logic>& ff_states,
                     std::vector<Logic>& values, const CompactFault* fault) {
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    values[sn.out] = ff_states.at(idx);
    apply_fault(values, fault, sn.out);
  }
}

void evaluate_combinational(const CompiledSimGraph& cg, std::vector<Logic>& values,
                            const CompactFault* fault, std::vector<Logic>& latches) {
  std::vector<Logic> ins;
  ins.reserve(6);
  const auto eval_node = [&](const SimNode& sn) {
    if (sn.type == GateType::INPUT || sn.type == GateType::DFF) {
      return;
    }
    if (sn.type == GateType::CONST0 || sn.type == GateType::CONST1) {
      values[sn.out] = logic_of(sn.type == GateType::CONST1);
      apply_fault(values, fault, sn.out);
      return;
    }
    if (is_icg(sn.type)) {
      const auto at = [&](uint32_t slot) {
        return slot != UNUSED_INPUT ? values[slot] : Logic::ZERO;
      };
      values[sn.out] = eval_icg_x(sn.type, at(sn.in0), logic_or(at(sn.in1), at(sn.in2)),
                                  latches[sn.out]);
    } else {
      ins.clear();
      for (uint32_t slot : {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5}) {
        if (slot != UNUSED_INPUT) {
          ins.push_back(values[slot]);
        }
      }
      values[sn.out] = eval_gate_x(sn.type, ins);
    }
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

Logic control_active(Logic value, Polarity polarity) {
  return polarity == Polarity::ACTIVE_HIGH ? (is_known(value) ? value : Logic::X)
                                           : logic_not(value);
}

Logic edge_active(Logic prev, Logic cur, TriggerType trigger) {
  if (trigger == TriggerType::POSEDGE) {
    return logic_and(logic_not(prev), cur);
  }
  return logic_and(prev, logic_not(cur));
}

// GoldenRefSim's update_ff_states with each decision an exact mux: clear and
// preset (gated off by scan shift) over the clock edge, the edge over hold or
// capture, and the capture's scan select over its data enable over D.
void update_ff_states(const CompiledSimGraph& cg, const std::vector<Logic>& values,
                      const std::vector<Logic>& prev_values, std::vector<Logic>& ff_states,
                      bool settled) {
  const std::vector<Logic>& sampled = settled ? prev_values : values;
  std::vector<Logic> next = ff_states;
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    const CompiledFFConfig& cfg = cg.ff_configs.at(sn.ff_cfg);
    const Logic q = ff_states[idx];
    const bool scan_mux = cfg.has_scan && sn.in4 != UNUSED_INPUT && sn.in5 != UNUSED_INPUT;
    const Logic scan_active =
        scan_mux ? control_active(values[sn.in5], cfg.scan_enable_polarity) : Logic::ZERO;
    const Logic not_shifting = logic_not(scan_active);
    const Logic clear =
        (cfg.has_clear && sn.in2 != UNUSED_INPUT)
            ? logic_and(not_shifting, control_active(values[sn.in2], cfg.clear_polarity))
            : Logic::ZERO;
    const Logic preset =
        (cfg.has_preset && sn.in3 != UNUSED_INPUT)
            ? logic_and(not_shifting, control_active(values[sn.in3], cfg.preset_polarity))
            : Logic::ZERO;

    Logic data = sampled[sn.in0];
    if (cfg.has_enable && sn.in2 != UNUSED_INPUT) {
      data = logic_mux(control_active(sampled[sn.in2], cfg.enable_polarity), q, data);
    }
    const Logic capture =
        scan_mux ? logic_mux(control_active(sampled[sn.in5], cfg.scan_enable_polarity),
                             data, sampled[sn.in4])
                 : data;
    const Logic edge = edge_active(prev_values[sn.in1], values[sn.in1], cfg.trigger);
    const Logic clocked = logic_mux(edge, q, capture);

    const Logic clear_value = logic_of(cfg.clear_value != 0);
    const Logic preset_value = logic_of(cfg.preset_value != 0);
    const Logic conflict_value = logic_of(cfg.clear_preset_conflict_value != 0);
    next[idx] = logic_mux(clear, logic_mux(preset, clocked, preset_value),
                          logic_mux(preset, clear_value, conflict_value));
  }
  ff_states = std::move(next);
}

}  // namespace

std::vector<XSnapshot> GoldenRefSimX::simulate_sequence(
    const CompiledSimGraph& cg, const TestVector& vec, const CompactFault* fault,
    const XRunOptions& options, const std::vector<int>* sample_yids) const {
  std::vector<Logic> values(cg.net_count, Logic::ZERO);
  std::vector<Logic> prev_values(cg.net_count, Logic::ZERO);
  std::vector<Logic> ff_states(cg.ff_configs.size(), options.initial_state);
  std::vector<Logic> latches(cg.net_count, options.initial_state);
  bool settled = false;
  for (const auto& [idx, value] : vec.initial_ff_state) {
    if (idx < ff_states.size()) {
      ff_states[idx] = logic_of(value);
    }
  }
  const CompactFault* active_fault =
      (fault != nullptr && fault->exclusion == FaultExclusion::NONE) ? fault : nullptr;
  std::vector<XSnapshot> samples;

  const std::vector<TestCycle> cycles = cycles_for(vec);
  for (size_t c = 0; c < cycles.size(); ++c) {
    const TestCycle& cycle = cycles[c];
    const CompactFault* cycle_fault = cycle.fault_active ? active_fault : nullptr;
    const std::set<int>* unknown =
        c < options.unknown_inputs.size() ? &options.unknown_inputs[c] : nullptr;
    broadcast_inputs(cg, cycle, unknown, values, cycle_fault);
    seed_ff_outputs(cg, ff_states, values, cycle_fault);
    evaluate_combinational(cg, values, cycle_fault, latches);
    update_ff_states(cg, values, prev_values, ff_states, settled);
    seed_ff_outputs(cg, ff_states, values, cycle_fault);
    evaluate_combinational(cg, values, cycle_fault, latches);
    for (int i = 0; i < cycle.settle_cycles; ++i) {
      seed_ff_outputs(cg, ff_states, values, cycle_fault);
      evaluate_combinational(cg, values, cycle_fault, latches);
    }
    if (cycle.sample_outputs) {
      samples.push_back(snapshot(cg, values, sample_yids));
    }
    prev_values = values;
    settled = true;
  }
  return samples;
}

XDetection classify_detection(const CompiledSimGraph& cg,
                              const std::vector<XSnapshot>& good,
                              const std::vector<XSnapshot>& faulty) {
  bool possible = false;
  const size_t count = std::min(good.size(), faulty.size());
  for (size_t i = 0; i < count; ++i) {
    for (int cidx : cg.observable) {
      const int yid = cg.compiled_to_yosys[cidx];
      const Logic g = good[i].at(yid);
      const Logic f = faulty[i].at(yid);
      if (!is_known(g)) {
        continue;
      }
      if (!is_known(f)) {
        possible = true;
      } else if (f != g) {
        return XDetection::DEFINITE;
      }
    }
  }
  return possible ? XDetection::POSSIBLE : XDetection::NONE;
}

}  // namespace faultflow
