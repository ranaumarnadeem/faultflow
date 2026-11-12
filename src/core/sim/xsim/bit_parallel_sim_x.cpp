#include "sim/xsim/bit_parallel_sim_x.hpp"

#include <stdexcept>

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

struct XState {
  std::vector<Dual> values;
  std::vector<Dual> prev_values;
  std::vector<Dual> ff_states;
  std::vector<Dual> latches;
  std::vector<char> fault_present;
  bool settled = false;
};

Dual broadcast(Logic v) {
  if (v == Logic::ZERO) {
    return dual_known(0ULL);
  }
  return v == Logic::ONE ? dual_known(~0ULL) : dual_x();
}

void inject_faults(std::vector<Dual>& nv, const FaultBatch& batch, uint32_t net) {
  for (int i = 0; i < batch.size; ++i) {
    const CompactFault& f = batch.faults[i];
    if (f.net_index != net) {
      continue;
    }
    if (f.type == FaultType::SA0) {
      nv[net].h &= ~f.sa_mask;
      nv[net].l |= f.sa_mask;
    } else {
      nv[net].h |= f.sa_mask;
      nv[net].l &= ~f.sa_mask;
    }
  }
}

void broadcast_inputs(XState& st, const CompiledSimGraph& cg, const TestCycle& cycle,
                      const std::set<int>* unknown, const FaultBatch& batch) {
  for (int pi : cg.pi_nets) {
    const int yid = cg.compiled_to_yosys[pi];
    if (unknown != nullptr && unknown->count(yid) != 0) {
      st.values[pi] = dual_x();
    } else {
      const auto it = cycle.inputs.find(yid);
      st.values[pi] = dual_known((it != cycle.inputs.end() && it->second) ? ~0ULL : 0ULL);
    }
  }
  // As BitParallelSim's cycle loop: every fault net, not only the inputs', so a
  // net no node recomputes (a pseudo-input) carries its fault too.
  for (int i = 0; i < batch.size; ++i) {
    inject_faults(st.values, batch, batch.faults[i].net_index);
  }
}

void seed_ff_outputs(XState& st, const CompiledSimGraph& cg, const FaultBatch& batch) {
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    st.values[sn.out] = st.ff_states.at(idx);
    inject_faults(st.values, batch, sn.out);
  }
}

Dual gather(const std::vector<Dual>& nv, uint32_t slot) {
  return slot == UNUSED_INPUT ? dual_known(0ULL) : nv[slot];
}

Dual active(Dual v, Polarity polarity);

// GoldenRefSimX's eval_latch over 64 lanes.
Dual eval_latch_dual(const CompiledSimGraph& cg, const SimNode& sn,
                     const std::array<Dual, 6>& ins, Dual& state) {
  const CompiledFFConfig& cfg = cg.latch_configs.at(sn.ff_cfg);
  const Dual zero = dual_known(0ULL);
  const Dual enable = active(ins[1], cfg.enable_polarity);
  const Dual clear = (cfg.has_clear && sn.in2 != UNUSED_INPUT)
                         ? active(ins[2], cfg.clear_polarity)
                         : zero;
  const Dual preset = (cfg.has_preset && sn.in3 != UNUSED_INPUT)
                          ? active(ins[3], cfg.preset_polarity)
                          : zero;
  const Dual open = dual_mux(enable, state, ins[0]);
  const auto constant = [](uint8_t v) { return dual_known(v != 0 ? ~0ULL : 0ULL); };
  state = dual_mux(clear, dual_mux(preset, open, constant(cfg.preset_value)),
                   dual_mux(preset, constant(cfg.clear_value),
                            constant(cfg.clear_preset_conflict_value)));
  return state;
}

// A tristate driver (GoldenRefSimX's eval_tbuf): its data (inverted for TINV)
// while enabled, Z -- (0,0), only ever on its private net -- while disabled, X
// while the enable is unknown.
Dual eval_tbuf_dual(GateType type, Dual data, Dual enable) {
  if (type == GateType::TINV_P || type == GateType::TINV_N) {
    data = dual_not(data);
  }
  if (type == GateType::TBUF_N || type == GateType::TINV_N) {
    enable = dual_not(enable);
  }
  const uint64_t on = enable.h & ~enable.l;
  const uint64_t unknown = enable.h & enable.l;
  return {(on & data.h) | unknown, (on & data.l) | unknown};
}

// A bus (eval_bus): the union of its drivers' rails -- Z yields, 0 with 1 is X;
// BUS_RES reads a floating bus as X.
Dual eval_bus_dual(const SimNode& sn, const std::array<Dual, 6>& ins) {
  Dual out{0ULL, 0ULL};
  const std::array<uint32_t, 6> slots{sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5};
  for (size_t i = 0; i < slots.size(); ++i) {
    if (slots[i] != UNUSED_INPUT) {
      out.h |= ins[i].h;
      out.l |= ins[i].l;
    }
  }
  if (sn.type == GateType::BUS_RES) {
    const uint64_t floating = ~(out.h | out.l);
    out.h |= floating;
    out.l |= floating;
  }
  return out;
}

void evaluate_combinational(XState& st, const CompiledSimGraph& cg,
                            const FaultBatch& batch) {
  auto& nv = st.values;
  auto& fp = st.fault_present;
  for (int i = 0; i < batch.size; ++i) {
    fp[batch.faults[i].net_index] = 1;
  }
  std::array<Dual, 6> ins{};
  // A node's value from its inputs now (a latch's or clock gate's state moves).
  const auto value_of = [&](const SimNode& sn) -> Dual {
    if (sn.type == GateType::CONST0 || sn.type == GateType::CONST1) {
      return dual_known(sn.type == GateType::CONST1 ? ~0ULL : 0ULL);
    }
    if (sn.type == GateType::CONSTX) {
      return dual_x();
    }
    ins[0] = gather(nv, sn.in0);
    ins[1] = gather(nv, sn.in1);
    ins[2] = gather(nv, sn.in2);
    ins[3] = gather(nv, sn.in3);
    ins[4] = gather(nv, sn.in4);
    ins[5] = gather(nv, sn.in5);
    if (sn.type == GateType::LATCH) {
      return eval_latch_dual(cg, sn, ins, st.latches[sn.out]);
    }
    if (sn.type == GateType::TBUF_P || sn.type == GateType::TBUF_N ||
        sn.type == GateType::TINV_P || sn.type == GateType::TINV_N) {
      return eval_tbuf_dual(sn.type, ins[0], ins[1]);
    }
    if (sn.type == GateType::BUS_MERGE || sn.type == GateType::BUS_RES) {
      return eval_bus_dual(sn, ins);
    }
    if (is_icg(sn.type)) {
      return eval_icg_dual(sn.type, ins[0], dual_or(ins[1], ins[2]), st.latches[sn.out]);
    }
    return eval_gate_dual(sn.type, ins);
  };
  // Writes a node's output, its faults on top.
  const auto write = [&](const SimNode& sn, Dual v) {
    nv[sn.out] = v;
    if (fp[sn.out]) {
      inject_faults(nv, batch, sn.out);
    }
  };
  const auto skipped = [](const SimNode& sn) {
    return sn.type == GateType::INPUT || sn.type == GateType::DFF;
  };
  const auto eval_level = [&](int start, int end) {
    for (int i = start; i < end; ++i) {
      const SimNode& sn = cg.nodes[i];
      if (!skipped(sn)) {
        write(sn, value_of(sn));
      }
    }
  };
  // A level holding a combinational loop: two-phase ternary simulation from the
  // nets' last values (Seger and Brzozowski), so a loop that holds a value keeps
  // it. Phase A only adds unknowns (each net the union of its old value and its
  // new one) until nothing changes; phase B, from there, only removes them. Each
  // lane settles at most once per net per phase; a level that would not (a
  // stateful cell on the loop) reads X.
  const auto settle_loop = [&](int start, int end) {
    const int cap = 2 * (end - start) + 4;
    for (int pass = 0;; ++pass) {
      bool changed = false;
      for (int i = start; i < end; ++i) {
        const SimNode& sn = cg.nodes[i];
        if (skipped(sn)) {
          continue;
        }
        const Dual old = nv[sn.out];
        const Dual now = value_of(sn);
        // The union; Z (a tristate driver's (0,0)) with a driven value is X.
        const uint64_t mixed = ~(old.h | old.l) ^ ~(now.h | now.l);
        write(sn, {old.h | now.h | mixed, old.l | now.l | mixed});
        changed = changed || nv[sn.out].h != old.h || nv[sn.out].l != old.l;
      }
      if (!changed) {
        break;
      }
      if (pass > cap) {
        throw std::logic_error("X engine: a loop's first phase did not settle");
      }
    }
    for (int pass = 0;; ++pass) {
      bool changed = false;
      for (int i = start; i < end; ++i) {
        const SimNode& sn = cg.nodes[i];
        if (skipped(sn)) {
          continue;
        }
        const Dual old = nv[sn.out];
        write(sn, value_of(sn));
        changed = changed || nv[sn.out].h != old.h || nv[sn.out].l != old.l;
      }
      if (!changed) {
        return;
      }
      if (pass > cap) {
        for (int i = start; i < end; ++i) {
          if (!skipped(cg.nodes[i])) {
            write(cg.nodes[i], dual_x());
          }
        }
        return;
      }
    }
  };
  if (cg.level_starts.size() >= 2) {
    for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
      if (lvl < cg.loop_levels.size() && cg.loop_levels[lvl] != 0) {
        settle_loop(cg.level_starts[lvl], cg.level_starts[lvl + 1]);
      } else {
        eval_level(cg.level_starts[lvl], cg.level_starts[lvl + 1]);
      }
    }
  } else {
    eval_level(0, static_cast<int>(cg.nodes.size()));
  }
  for (int i = 0; i < batch.size; ++i) {
    fp[batch.faults[i].net_index] = 0;
  }
}

Dual active(Dual v, Polarity polarity) {
  return polarity == Polarity::ACTIVE_HIGH ? v : dual_not(v);
}

// GoldenRefSimX's update_ff_states over 64 lanes.
void update_ff_states(XState& st, const CompiledSimGraph& cg) {
  const auto& nv = st.values;
  const auto& sampled = st.settled ? st.prev_values : nv;
  std::vector<Dual> next = st.ff_states;
  for (size_t idx = 0; idx < cg.ff_nodes.size(); ++idx) {
    const SimNode& sn = cg.nodes.at(cg.ff_nodes[idx]);
    const CompiledFFConfig& cfg = cg.ff_configs.at(sn.ff_cfg);
    const Dual q = st.ff_states[idx];
    const bool scan_mux = cfg.has_scan && sn.in4 != UNUSED_INPUT && sn.in5 != UNUSED_INPUT;
    const Dual zero = dual_known(0ULL);
    const Dual scan_active = scan_mux ? active(nv[sn.in5], cfg.scan_enable_polarity) : zero;
    const Dual not_shifting = dual_not(scan_active);
    const Dual clear = (cfg.has_clear && sn.in2 != UNUSED_INPUT)
                           ? dual_and(not_shifting, active(nv[sn.in2], cfg.clear_polarity))
                           : zero;
    const Dual preset = (cfg.has_preset && sn.in3 != UNUSED_INPUT)
                            ? dual_and(not_shifting, active(nv[sn.in3], cfg.preset_polarity))
                            : zero;
    Dual data = sampled[sn.in0];
    if (cfg.has_enable && sn.in2 != UNUSED_INPUT) {
      data = dual_mux(active(sampled[sn.in2], cfg.enable_polarity), q, data);
    }
    const Dual capture =
        scan_mux ? dual_mux(active(sampled[sn.in5], cfg.scan_enable_polarity), data,
                            sampled[sn.in4])
                 : data;
    const Dual prev_clk = st.prev_values[sn.in1];
    const Dual cur_clk = nv[sn.in1];
    const Dual edge = cfg.trigger == TriggerType::POSEDGE
                          ? dual_and(dual_not(prev_clk), cur_clk)
                          : dual_and(prev_clk, dual_not(cur_clk));
    const Dual clocked = dual_mux(edge, q, capture);
    const Dual clear_value = dual_known(cfg.clear_value != 0 ? ~0ULL : 0ULL);
    const Dual preset_value = dual_known(cfg.preset_value != 0 ? ~0ULL : 0ULL);
    const Dual conflict_value =
        dual_known(cfg.clear_preset_conflict_value != 0 ? ~0ULL : 0ULL);
    next[idx] = dual_mux(clear, dual_mux(preset, clocked, preset_value),
                         dual_mux(preset, clear_value, conflict_value));
  }
  st.ff_states = std::move(next);
}

// The cycle loop of BitParallelSim's for_each_sample: on_sample(net values) at
// each sampled cycle.
template <typename OnSample>
void for_each_sample(const CompiledSimGraph& cg, const TestVector& vec,
                     const FaultBatch& batch, const XRunOptions& options,
                     OnSample&& on_sample) {
  XState st;
  st.values.assign(cg.net_count, dual_known(0ULL));
  st.prev_values.assign(cg.net_count, dual_known(0ULL));
  st.ff_states.assign(cg.ff_configs.size(), broadcast(options.initial_state));
  st.latches.assign(cg.net_count, broadcast(options.initial_state));
  st.fault_present.assign(cg.net_count, 0);
  // A combinational loop's nets power up as a flop does.
  for (size_t i = 0; i < cg.in_loop.size(); ++i) {
    if (cg.in_loop[i] != 0) {
      st.values[cg.nodes[i].out] = broadcast(options.initial_state);
    }
  }
  for (const auto& [idx, value] : vec.initial_ff_state) {
    if (idx < st.ff_states.size()) {
      st.ff_states[idx] = dual_known(value ? ~0ULL : 0ULL);
    }
  }
  std::vector<TestCycle> single;
  const std::vector<TestCycle>* cycles = &vec.cycles;
  if (vec.cycles.empty()) {
    TestCycle c;
    c.inputs = vec.inputs;
    c.sample_outputs = true;
    single.push_back(c);
    cycles = &single;
  }
  const FaultBatch inactive;
  for (size_t c = 0; c < cycles->size(); ++c) {
    const TestCycle& cycle = (*cycles)[c];
    const FaultBatch& b = cycle.fault_active ? batch : inactive;
    const std::set<int>* unknown =
        c < options.unknown_inputs.size() ? &options.unknown_inputs[c] : nullptr;
    broadcast_inputs(st, cg, cycle, unknown, b);
    seed_ff_outputs(st, cg, b);
    evaluate_combinational(st, cg, b);
    update_ff_states(st, cg);
    seed_ff_outputs(st, cg, b);
    evaluate_combinational(st, cg, b);
    for (int i = 0; i < cycle.settle_cycles; ++i) {
      seed_ff_outputs(st, cg, b);
      evaluate_combinational(st, cg, b);
    }
    if (cycle.sample_outputs) {
      on_sample(st.values);
    }
    st.prev_values = st.values;
    st.settled = true;
  }
}

}  // namespace

std::vector<std::vector<Dual>> BitParallelSimX::simulate_batch_samples(
    const CompiledSimGraph& cg, const TestVector& vec, const FaultBatch& batch,
    const XRunOptions& options) const {
  std::vector<std::vector<Dual>> samples;
  for_each_sample(cg, vec, batch, options,
                  [&](const std::vector<Dual>& nv) { samples.push_back(nv); });
  return samples;
}

std::vector<std::vector<Dual>> BitParallelSimX::simulate_batch_samples(
    const CompiledSimGraph& cg, const TestVector& vec, const FaultBatch& batch,
    const XRunOptions& options, const std::vector<uint32_t>& nets) const {
  std::vector<std::vector<Dual>> samples;
  for_each_sample(cg, vec, batch, options, [&](const std::vector<Dual>& nv) {
    std::vector<Dual> row;
    row.reserve(nets.size());
    for (uint32_t net : nets) {
      row.push_back(nv.at(net));
    }
    samples.push_back(std::move(row));
  });
  return samples;
}

namespace {

// One observed net's detections: lanes showing a 0 against the good lane's 1 (or
// the reverse), and lanes X where the good lane is known.
XBatchDetection detections_at(Dual f) {
  // The good machine (lane 0) broadcast: g1 where it is 1, g0 where it is 0.
  const uint64_t g1 = ((f.h & 1ULL) && !(f.l & 1ULL)) ? ~0ULL : 0ULL;
  const uint64_t g0 = ((f.l & 1ULL) && !(f.h & 1ULL)) ? ~0ULL : 0ULL;
  const uint64_t f1 = f.h & ~f.l;
  const uint64_t f0 = f.l & ~f.h;
  return {(g1 & f0) | (g0 & f1), (g1 | g0) & f.h & f.l};
}

Logic lane0(Dual d) {
  const bool h = (d.h & 1ULL) != 0;
  const bool l = (d.l & 1ULL) != 0;
  return h && l ? Logic::X : logic_of(h);
}

}  // namespace

XBatchDetection BitParallelSimX::detect_batch(const CompiledSimGraph& cg,
                                              const TestVector& vec,
                                              const FaultBatch& batch,
                                              const XRunOptions& options) const {
  XBatchDetection out;
  for_each_sample(cg, vec, batch, options, [&](const std::vector<Dual>& nv) {
    for (int obs : cg.observable) {
      const XBatchDetection d = detections_at(nv[static_cast<size_t>(obs)]);
      out.definite |= d.definite;
      out.possible |= d.possible;
    }
  });
  out.definite &= batch.mask;
  out.possible &= batch.mask & ~out.definite;
  return out;
}

BitParallelSimX::Observed BitParallelSimX::detect_batch_observed(
    const CompiledSimGraph& cg, const TestVector& vec, const FaultBatch& batch,
    const std::vector<uint32_t>& observe, const XRunOptions& options) const {
  Observed out;
  out.first_definite.fill(-1);
  out.first_possible.fill(-1);
  uint64_t seen_definite = 0ULL;
  uint64_t seen_possible = 0ULL;
  int32_t index = 0;
  const auto record = [&](uint64_t lanes, uint64_t& seen, std::array<int32_t, 64>& first) {
    const uint64_t fresh = lanes & batch.mask & ~seen;
    for (int lane = 1; lane < 64; ++lane) {
      if ((fresh >> lane) & 1ULL) {
        first[static_cast<size_t>(lane)] = index;
      }
    }
    seen |= fresh;
  };
  for_each_sample(cg, vec, batch, options, [&](const std::vector<Dual>& nv) {
    XBatchDetection here;
    std::vector<Logic> values;
    values.reserve(observe.size());
    for (uint32_t obs : observe) {
      const XBatchDetection d = detections_at(nv[obs]);
      here.definite |= d.definite;
      here.possible |= d.possible;
      values.push_back(lane0(nv[obs]));
    }
    record(here.definite, seen_definite, out.first_definite);
    record(here.possible, seen_possible, out.first_possible);
    out.lane0.push_back(std::move(values));
    ++index;
  });
  return out;
}

}  // namespace faultflow
