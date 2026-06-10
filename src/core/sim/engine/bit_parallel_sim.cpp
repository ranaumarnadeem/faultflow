#include "sim/engine/bit_parallel_sim.hpp"

#include "sim/gate_eval.hpp"

namespace faultflow {
namespace {

uint64_t gather_input(const std::vector<uint64_t>& nv, uint32_t slot) {
  return slot == UNUSED_INPUT ? 0ULL : nv[slot];
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

uint64_t active_mask(uint64_t value, Polarity polarity) {
  return polarity == Polarity::ACTIVE_HIGH ? value : ~value;
}

uint64_t edge_mask(uint64_t prev, uint64_t cur, TriggerType trigger) {
  if (trigger == TriggerType::POSEDGE) {
    return ~prev & cur;
  }
  return prev & ~cur;
}

uint64_t apply_value_mask(uint64_t original, uint64_t mask, uint8_t value) {
  return value == 0 ? (original & ~mask) : (original | mask);
}

}  // namespace

void BitParallelSim::broadcast_inputs(SimState& state, const CompiledSimGraph& cg,
                                    const TestVector& vec) const {
  TestCycle cycle;
  cycle.inputs = vec.inputs;
  broadcast_cycle_inputs(state, cg, cycle);
}

void BitParallelSim::broadcast_cycle_inputs(SimState& state,
                                            const CompiledSimGraph& cg,
                                            const TestCycle& cycle) const {
  auto& nv = state.current_values();
  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    const bool val = cycle.inputs.count(yid) ? cycle.inputs.at(yid) : false;
    nv[pi_idx] = val ? ~0ULL : 0ULL;
  }
}

void BitParallelSim::inject_faults(std::vector<uint64_t>& nv,
                                   const FaultBatch& batch,
                                   uint32_t net_index) const {
  for (int i = 0; i < batch.size; ++i) {
    const CompactFault& f = batch.faults[i];
    if (f.net_index != net_index) {
      continue;
    }
    if (f.type == FaultType::SA0) {
      nv[net_index] &= ~f.sa_mask;
    } else {
      nv[net_index] |= f.sa_mask;
    }
  }
}

void BitParallelSim::evaluate_combinational(SimState& state,
                                            const CompiledSimGraph& cg,
                                            const FaultBatch& batch) const {
  auto& nv = state.current_values();

  const auto eval_level = [&](int start, int end) {
    for (int i = start; i < end; ++i) {
      const SimNode& sn = cg.nodes[i];
      if (sn.type == GateType::INPUT || sn.type == GateType::DFF) {
        continue;
      }
      if (sn.type == GateType::CONST0) {
        nv[sn.out] = 0ULL;
        inject_faults(nv, batch, sn.out);
        continue;
      }
      if (sn.type == GateType::CONST1) {
        nv[sn.out] = ~0ULL;
        inject_faults(nv, batch, sn.out);
        continue;
      }
      const std::vector<uint64_t> ins = {
          gather_input(nv, sn.in0), gather_input(nv, sn.in1),
          gather_input(nv, sn.in2), gather_input(nv, sn.in3),
          gather_input(nv, sn.in4), gather_input(nv, sn.in5)};
      nv[sn.out] = eval_gate(sn.type, ins);
      inject_faults(nv, batch, sn.out);
    }
  };

  if (cg.level_starts.size() >= 2) {
    for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
      eval_level(cg.level_starts[lvl], cg.level_starts[lvl + 1]);
    }
    return;
  }

  eval_level(0, static_cast<int>(cg.nodes.size()));
}

void BitParallelSim::seed_ff_outputs(SimState& state, const CompiledSimGraph& cg,
                                     const FaultBatch& batch) const {
  auto& nv = state.current_values();
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    nv[sn.out] = state.ff_states.at(idx);
    inject_faults(nv, batch, sn.out);
  }
}

void BitParallelSim::update_ff_states(SimState& state,
                                      const CompiledSimGraph& cg) const {
  const auto& nv = state.current_values();
  std::vector<uint64_t> next = state.ff_states;
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    const CompiledFFConfig& cfg = cg.ff_configs.at(sn.ff_cfg);
    const uint64_t clear =
        (cfg.has_clear && sn.in2 != UNUSED_INPUT)
            ? active_mask(nv[sn.in2], cfg.clear_polarity)
            : 0ULL;
    const uint64_t preset =
        (cfg.has_preset && sn.in3 != UNUSED_INPUT)
            ? active_mask(nv[sn.in3], cfg.preset_polarity)
            : 0ULL;
    const uint64_t conflict = clear & preset;
    const uint64_t clear_only = clear & ~preset;
    const uint64_t preset_only = preset & ~clear;
    const uint64_t edge =
        edge_mask(state.prev_values[sn.in1], nv[sn.in1], cfg.trigger) &
        ~(clear | preset);
    uint64_t capture = nv[sn.in0];
    if (cfg.has_scan && sn.in4 != UNUSED_INPUT && sn.in5 != UNUSED_INPUT) {
      const uint64_t scan_active =
          active_mask(nv[sn.in5], cfg.scan_enable_polarity);
      capture = (capture & ~scan_active) | (nv[sn.in4] & scan_active);
    }

    uint64_t value = next[idx];
    value = apply_value_mask(value, conflict, cfg.clear_preset_conflict_value);
    value = apply_value_mask(value, clear_only, cfg.clear_value);
    value = apply_value_mask(value, preset_only, cfg.preset_value);
    value = (value & ~edge) | (capture & edge);
    next[idx] = value;
  }
  state.ff_states = std::move(next);
}

uint64_t BitParallelSim::check_observation(const SimState& state,
                                           const CompiledSimGraph& cg,
                                           const FaultBatch& batch) const {
  const auto& nv = state.current_values();
  uint64_t detected = 0;
  for (int obs : cg.observable) {
    const uint64_t word = nv[obs];
    const uint64_t ff = (word & 1ULL) ? ~0ULL : 0ULL;
    detected |= (word ^ ff);
  }
  return detected & batch.mask;
}

bool BitParallelSim::simulate_single_fault(const CompiledSimGraph& cg,
                                           const TestVector& vec,
                                           const CompactFault& fault) const {
  if (fault.exclusion != FaultExclusion::NONE) {
    return false;
  }
  FaultBatch batch;
  CompactFault f = fault;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  batch.faults[0] = f;
  batch.size = 1;
  batch.mask = f.sa_mask;

  return simulate_batch(cg, vec, batch) != 0;
}

uint64_t BitParallelSim::simulate_batch(const CompiledSimGraph& cg,
                                        const TestVector& vec,
                                        const FaultBatch& batch) const {
  SimState state;
  state.init(cg.net_count, 1, static_cast<int>(cg.ff_configs.size()));
  for (const auto& [idx, value] : vec.initial_ff_state) {
    if (idx < state.initial_ff_state.size()) {
      state.initial_ff_state[idx] = value ? ~0ULL : 0ULL;
    }
  }
  state.reset_ff_states();

  uint64_t detected = 0ULL;
  for (const TestCycle& cycle : cycles_for(vec)) {
    broadcast_cycle_inputs(state, cg, cycle);
    for (int i = 0; i < batch.size; ++i) {
      inject_faults(state.current_values(), batch, batch.faults[i].net_index);
    }
    seed_ff_outputs(state, cg, batch);
    evaluate_combinational(state, cg, batch);
    update_ff_states(state, cg);
    seed_ff_outputs(state, cg, batch);
    evaluate_combinational(state, cg, batch);
    for (int i = 0; i < cycle.settle_cycles; ++i) {
      seed_ff_outputs(state, cg, batch);
      evaluate_combinational(state, cg, batch);
    }
    if (cycle.sample_outputs) {
      detected |= check_observation(state, cg, batch);
    }
    state.prev_values = state.current_values();
  }
  return detected & batch.mask;
}

}  // namespace faultflow
