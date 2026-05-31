#include "sim/engine/bit_parallel_sim.hpp"

#include "sim/gate_eval.hpp"

namespace faultflow {
namespace {

uint64_t gather_input(const std::vector<uint64_t>& nv, uint32_t slot) {
  return slot == UNUSED_INPUT ? 0ULL : nv[slot];
}

}  // namespace

void BitParallelSim::broadcast_inputs(SimState& state, const CompiledSimGraph& cg,
                                    const TestVector& vec) const {
  auto& nv = state.current_values();
  for (int pi_idx : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi_idx];
    const bool val = vec.inputs.count(yid) ? vec.inputs.at(yid) : false;
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
      if (sn.type == GateType::INPUT) {
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

  SimState state;
  state.init(cg.net_count);
  broadcast_inputs(state, cg, vec);
  inject_faults(state.current_values(), batch, f.net_index);
  evaluate_combinational(state, cg, batch);
  return check_observation(state, cg, batch) != 0;
}

uint64_t BitParallelSim::simulate_batch(const CompiledSimGraph& cg,
                                        const TestVector& vec,
                                        const FaultBatch& batch) const {
  SimState state;
  state.init(cg.net_count);
  broadcast_inputs(state, cg, vec);
  for (int i = 0; i < batch.size; ++i) {
    inject_faults(state.current_values(), batch, batch.faults[i].net_index);
  }
  evaluate_combinational(state, cg, batch);
  return check_observation(state, cg, batch);
}

}  // namespace faultflow
