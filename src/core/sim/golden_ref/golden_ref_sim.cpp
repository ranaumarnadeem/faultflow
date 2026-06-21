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

// Restricted snapshot: only the requested Yosys net IDs. Used by the scan
// pattern sim, which reads a handful of ports out of a large graph each cycle,
// so building a full net-wide map per cycle is wasted work.
std::map<int, bool> snapshot_sampled(const CompiledSimGraph& cg,
                                     const std::vector<bool>& values,
                                     const std::vector<int>& sample_yids) {
  std::map<int, bool> out;
  for (int yid : sample_yids) {
    auto it = cg.yosys_to_compiled.find(yid);
    if (it != cg.yosys_to_compiled.end()) {
      out[yid] = values[static_cast<size_t>(it->second)];
    }
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
                            const CompactFault* fault,
                            const ModeConfig* mc = nullptr) {
  // Reused across nodes so the hot per-node eval does not heap-allocate a fresh
  // vector each time (the scan-pattern sim runs this thousands of times over a
  // large graph; the per-node alloc dominated the runtime).
  std::vector<bool> ins;
  ins.reserve(6);
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
    // Wrapper cells: FUNCTIONAL (mc == nullptr) is a transparent buffer of CFI
    // (in0); under a ModeConfig a native scan WBR drives its functional output
    // from the FF state q (in1) on its active side, or safe-0 on the inactive
    // side. Handled here so the sequential scan-protocol gate is mode-faithful.
    if (sn.type == GateType::WBR_IN || sn.type == GateType::WBR_OUT) {
      const uint8_t act = (mc != nullptr)
                              ? mc->wbr_action[sn.out]
                              : static_cast<uint8_t>(WbrAction::PASS);
      if (act == static_cast<uint8_t>(WbrAction::SKIP_STIMULUS)) {
        // control point: keep the value already in place.
      } else if (act == static_cast<uint8_t>(WbrAction::FORCE_ZERO)) {
        values[sn.out] = false;
      } else if (act == static_cast<uint8_t>(WbrAction::DRIVE_FROM_FF)) {
        values[sn.out] = (sn.in1 != UNUSED_INPUT) ? values[sn.in1] : false;
      } else {
        values[sn.out] = (sn.in0 != UNUSED_INPUT) ? values[sn.in0] : false;
      }
      apply_fault(values, fault, sn.out);
      return;
    }
    ins.clear();
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

// IEEE 1500 mode-aware combinational evaluation (scalar oracle). Returns the
// settled net values. PIs and ModeConfig stimulus nets are driven from the
// vector; wrapper cells apply their per-mode action (buffer / retain stimulus /
// safe-0). A net fault, if any, overrides after each write.
std::vector<bool> run_comb_mode(const CompiledSimGraph& cg, const TestVector& vec,
                                const CompactFault* fault, const ModeConfig& mc) {
  std::vector<bool> values(cg.net_count, false);

  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    auto it = vec.inputs.find(yid);
    values[pi_idx] = (it != vec.inputs.end()) ? it->second : false;
    apply_fault(values, fault, static_cast<uint32_t>(pi_idx));
  }
  for (uint32_t s : mc.stimulus_nets) {
    const int yid = cg.compiled_to_yosys[s];
    auto it = vec.inputs.find(yid);
    values[s] = (it != vec.inputs.end()) ? it->second : false;
    apply_fault(values, fault, s);
  }

  const auto eval_one = [&](const SimNode& sn) {
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
    if (sn.type == GateType::WBR_IN || sn.type == GateType::WBR_OUT) {
      const uint8_t act = mc.wbr_action[sn.out];
      if (act == static_cast<uint8_t>(WbrAction::SKIP_STIMULUS)) {
        // control point: keep the broadcast stimulus value.
      } else if (act == static_cast<uint8_t>(WbrAction::FORCE_ZERO)) {
        values[sn.out] = false;
      } else if (act == static_cast<uint8_t>(WbrAction::DRIVE_FROM_FF)) {
        values[sn.out] =
            (sn.in1 != UNUSED_INPUT) ? values[sn.in1] : false;  // FF state q
      } else {
        values[sn.out] = (sn.in0 != UNUSED_INPUT) ? values[sn.in0] : false;
      }
      apply_fault(values, fault, sn.out);
      return;
    }
    std::vector<bool> ins;
    const uint32_t in_slots[] = {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5};
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
        eval_one(cg.nodes[i]);
      }
    }
  } else {
    for (const auto& sn : cg.nodes) {
      eval_one(sn);
    }
  }
  return values;
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
      bool capture = values[sn.in0];
      if (cfg.has_scan && sn.in4 != UNUSED_INPUT && sn.in5 != UNUSED_INPUT &&
          control_active(values[sn.in5], cfg.scan_enable_polarity)) {
        capture = values[sn.in4];
      }
      next[idx] = capture;
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

namespace {

// Shared sequential driver. When sample_yids is non-null each sampled cycle is
// snapshotted to only those Yosys net IDs (fast path for the scan-pattern sim);
// when null the full net-wide snapshot is taken (general/golden behaviour).
std::vector<std::map<int, bool>> run_sequence(const CompiledSimGraph& cg,
                                              const TestVector& vec,
                                              const CompactFault& fault,
                                              const std::vector<int>* sample_yids) {
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

  // Mode-faithful scan-protocol replay: with native shiftable WBR cells the
  // boundary mode-mux must drive the core/interconnect from the loaded FF state
  // q in INTEST/EXTEST. Build the ModeConfig once (mode is fixed for the run).
  const bool use_mode = vec.test_mode != TestMode::FUNCTIONAL &&
                        !cg.wrapper_cells.empty();
  const ModeConfig mode_cfg =
      use_mode ? build_mode_config(cg, vec.test_mode) : ModeConfig{};
  const ModeConfig* mcp = use_mode ? &mode_cfg : nullptr;

  for (const TestCycle& cycle : cycles_for(vec)) {
    const CompactFault* cycle_fault = cycle.fault_active ? active_fault : nullptr;
    broadcast_inputs(cg, cycle, values, cycle_fault);
    seed_ff_outputs(cg, ff_states, values, cycle_fault);
    evaluate_combinational(cg, values, cycle_fault, mcp);
    update_ff_states(cg, values, prev_values, ff_states);
    seed_ff_outputs(cg, ff_states, values, cycle_fault);
    evaluate_combinational(cg, values, cycle_fault, mcp);
    for (int i = 0; i < cycle.settle_cycles; ++i) {
      seed_ff_outputs(cg, ff_states, values, cycle_fault);
      evaluate_combinational(cg, values, cycle_fault, mcp);
    }
    if (cycle.sample_outputs) {
      samples.push_back(sample_yids ? snapshot_sampled(cg, values, *sample_yids)
                                    : snapshot(cg, values));
    }
    for (size_t i = 0; i < values.size(); ++i) {
      prev_values[i] = values[i];
    }
  }
  return samples;
}

}  // namespace

std::vector<std::map<int, bool>> GoldenRefSim::simulate_sequence_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec) const {
  CompactFault none;
  none.exclusion = FaultExclusion::BLACKBOX;
  return run_sequence(cg, vec, none, nullptr);
}

std::vector<std::map<int, bool>> GoldenRefSim::simulate_sequence_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec,
    const std::vector<int>& sample_yids) const {
  CompactFault none;
  none.exclusion = FaultExclusion::BLACKBOX;
  return run_sequence(cg, vec, none, &sample_yids);
}

std::vector<std::map<int, bool>> GoldenRefSim::simulate_sequence_with_fault(
    const CompiledSimGraph& cg, const TestVector& vec,
    const CompactFault& fault) const {
  return run_sequence(cg, vec, fault, nullptr);
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

std::map<int, bool> GoldenRefSim::simulate_fault_free(
    const CompiledSimGraph& cg, const TestVector& vec,
    const ModeConfig& mode) const {
  return snapshot(cg, run_comb_mode(cg, vec, nullptr, mode));
}

std::map<int, bool> GoldenRefSim::simulate_with_fault(
    const CompiledSimGraph& cg, const TestVector& vec, const CompactFault& fault,
    const ModeConfig& mode) const {
  const CompactFault* f =
      fault.exclusion == FaultExclusion::NONE ? &fault : nullptr;
  return snapshot(cg, run_comb_mode(cg, vec, f, mode));
}

bool GoldenRefSim::is_detected(const CompiledSimGraph& cg,
                               const std::map<int, bool>& fault_free,
                               const std::map<int, bool>& faulty,
                               const ModeConfig& mode) const {
  for (uint32_t cidx : mode.observable_nets) {
    const int yid = cg.compiled_to_yosys[cidx];
    auto a = fault_free.find(yid);
    auto b = faulty.find(yid);
    if (a != fault_free.end() && b != faulty.end() && a->second != b->second) {
      return true;
    }
  }
  return false;
}

bool GoldenRefSim::simulate_transition_fault(const CompiledSimGraph& cg,
                                              const TestVector& v1,
                                              const TestVector& v2,
                                              const CompactFault& fault) const {
  if (fault.exclusion != FaultExclusion::NONE) {
    return false;
  }

  TestCycle init_cycle;
  init_cycle.inputs = v1.inputs;
  init_cycle.sample_outputs = true;
  init_cycle.fault_active = false;

  TestCycle capture_cycle;
  capture_cycle.inputs = v2.inputs;
  capture_cycle.sample_outputs = true;
  capture_cycle.fault_active = true;

  TestVector combined;
  combined.cycles = {init_cycle, capture_cycle};

  const auto ff_samples = simulate_sequence_fault_free(cg, combined);
  if (ff_samples.size() < 2) {
    return false;
  }

  // Transition check: good machine must have made the required transition at the fault net.
  const int target_yid = cg.compiled_to_yosys[fault.net_index];
  const auto& init_snap = ff_samples[0];
  const auto& cap_snap = ff_samples[1];
  if (init_snap.count(target_yid) == 0 || cap_snap.count(target_yid) == 0) {
    return false;
  }
  // STR (SA0): init=0 -> cap=1;  STF (SA1): init=1 -> cap=0.
  const bool pre_val = (fault.type == FaultType::SA0) ? false : true;
  const bool post_val = (fault.type == FaultType::SA0) ? true : false;
  if (init_snap.at(target_yid) != pre_val || cap_snap.at(target_yid) != post_val) {
    return false;
  }

  const auto faulty_samples = simulate_sequence_with_fault(cg, combined, fault);
  return is_sequence_detected(cg, ff_samples, faulty_samples);
}

}  // namespace faultflow
