#include "sim/xsim/golden_ref_sim_x.hpp"

#include <algorithm>
#include <stdexcept>

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

Logic control_active(Logic value, Polarity polarity);

// A tristate driver: its data (inverted for TINV) while enabled, Z while
// disabled, X while the enable is unknown.
Logic eval_tbuf(GateType type, Logic data, Logic enable) {
  if (type == GateType::TINV_P || type == GateType::TINV_N) {
    data = logic_not(data);
  }
  const Polarity level = (type == GateType::TBUF_N || type == GateType::TINV_N)
                             ? Polarity::ACTIVE_LOW
                             : Polarity::ACTIVE_HIGH;
  const Logic on = control_active(enable, level);
  if (on == Logic::ONE) {
    return is_known(data) ? data : Logic::X;
  }
  return on == Logic::ZERO ? Logic::Z : Logic::X;
}

// A bus: Z yields to a driven value, two different values (or an unknown) are
// X; BUS_RES reads a floating bus (every driver Z) as X, BUS_MERGE keeps Z.
Logic eval_bus(GateType type, const std::vector<Logic>& drivers) {
  Logic out = Logic::Z;
  for (Logic v : drivers) {
    if (v == Logic::Z) {
      continue;
    }
    if (out == Logic::Z) {
      out = is_known(v) ? v : Logic::X;
    } else if (!(is_known(v) && v == out)) {
      out = Logic::X;
    }
  }
  return (type == GateType::BUS_RES && out == Logic::Z) ? Logic::X : out;
}

// A latch (GateType LATCH): transparent (D) while its enable is active, the held
// value otherwise, clear and preset forcing it as a flop's do -- each an exact
// mux, so an unknown enable keeps a value only where D and the held one agree.
Logic eval_latch(const CompiledSimGraph& cg, const SimNode& sn,
                 const std::vector<Logic>& values, Logic& state) {
  const CompiledFFConfig& cfg = cg.latch_configs.at(sn.ff_cfg);
  const auto at = [&](uint32_t slot) {
    return slot != UNUSED_INPUT ? values[slot] : Logic::ZERO;
  };
  const Logic enable = control_active(at(sn.in1), cfg.enable_polarity);
  const Logic clear = (cfg.has_clear && sn.in2 != UNUSED_INPUT)
                          ? control_active(at(sn.in2), cfg.clear_polarity)
                          : Logic::ZERO;
  const Logic preset = (cfg.has_preset && sn.in3 != UNUSED_INPUT)
                           ? control_active(at(sn.in3), cfg.preset_polarity)
                           : Logic::ZERO;
  const Logic open = logic_mux(enable, state, at(sn.in0));
  state = logic_mux(clear, logic_mux(preset, open, logic_of(cfg.preset_value != 0)),
                    logic_mux(preset, logic_of(cfg.clear_value != 0),
                              logic_of(cfg.clear_preset_conflict_value != 0)));
  return state;
}

void evaluate_combinational(const CompiledSimGraph& cg, std::vector<Logic>& values,
                            const CompactFault* fault, std::vector<Logic>& latches) {
  std::vector<Logic> ins;
  ins.reserve(6);
  const auto skipped = [](const SimNode& sn) {
    return sn.type == GateType::INPUT || sn.type == GateType::DFF;
  };
  // A node's value from its inputs now (a latch's or clock gate's state moves).
  const auto value_of = [&](const SimNode& sn) -> Logic {
    if (sn.type == GateType::CONST0 || sn.type == GateType::CONST1) {
      return logic_of(sn.type == GateType::CONST1);
    }
    if (sn.type == GateType::CONSTX) {
      return Logic::X;
    }
    if (sn.type == GateType::LATCH) {
      return eval_latch(cg, sn, values, latches[sn.out]);
    }
    const auto at = [&](uint32_t slot) {
      return slot != UNUSED_INPUT ? values[slot] : Logic::ZERO;
    };
    if (sn.type == GateType::TBUF_P || sn.type == GateType::TBUF_N ||
        sn.type == GateType::TINV_P || sn.type == GateType::TINV_N) {
      return eval_tbuf(sn.type, at(sn.in0), at(sn.in1));
    }
    if (sn.type == GateType::BUS_MERGE || sn.type == GateType::BUS_RES) {
      std::vector<Logic> drivers;
      for (uint32_t slot : {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5}) {
        if (slot != UNUSED_INPUT) {
          drivers.push_back(values[slot]);
        }
      }
      return eval_bus(sn.type, drivers);
    }
    if (is_icg(sn.type)) {
      return eval_icg_x(sn.type, at(sn.in0), logic_or(at(sn.in1), at(sn.in2)),
                        latches[sn.out]);
    }
    ins.clear();
    for (uint32_t slot : {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5}) {
      if (slot != UNUSED_INPUT) {
        ins.push_back(values[slot]);
      }
    }
    return eval_gate_x(sn.type, ins);
  };
  const auto write = [&](const SimNode& sn, Logic v) {
    values[sn.out] = v;
    apply_fault(values, fault, sn.out);
  };
  const auto eval_node = [&](const SimNode& sn) {
    if (!skipped(sn)) {
      write(sn, value_of(sn));
    }
  };
  // A loop's level: BitParallelSimX's settle_loop, one lane -- phase A only adds
  // unknowns (the union of old and new: equal, or X), phase B only removes them.
  const auto settle_loop = [&](int start, int end) {
    const int cap = 2 * (end - start) + 4;
    for (int pass = 0;; ++pass) {
      bool changed = false;
      for (int i = start; i < end; ++i) {
        const SimNode& sn = cg.nodes[i];
        if (skipped(sn)) {
          continue;
        }
        const Logic old = values[sn.out];
        const Logic now = value_of(sn);
        write(sn, old == now ? old : Logic::X);
        changed = changed || values[sn.out] != old;
      }
      if (!changed) {
        break;
      }
      if (pass > cap) {
        throw std::logic_error("X reference: a loop's first phase did not settle");
      }
    }
    for (int pass = 0;; ++pass) {
      bool changed = false;
      for (int i = start; i < end; ++i) {
        const SimNode& sn = cg.nodes[i];
        if (skipped(sn)) {
          continue;
        }
        const Logic old = values[sn.out];
        write(sn, value_of(sn));
        changed = changed || values[sn.out] != old;
      }
      if (!changed) {
        return;
      }
      if (pass > cap) {
        for (int i = start; i < end; ++i) {
          if (!skipped(cg.nodes[i])) {
            write(cg.nodes[i], Logic::X);
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
        continue;
      }
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
  // A combinational loop's nets power up as a flop does.
  for (size_t i = 0; i < cg.in_loop.size(); ++i) {
    if (cg.in_loop[i] != 0) {
      values[cg.nodes[i].out] = options.initial_state;
    }
  }
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

TestVector transition_pair(const TestVector& v1, const TestVector& v2) {
  TestCycle launch;
  launch.inputs = v1.inputs;
  launch.sample_outputs = true;
  launch.fault_active = false;
  TestCycle capture;
  capture.inputs = v2.inputs;
  capture.sample_outputs = true;
  capture.fault_active = true;
  TestVector pair;
  pair.cycles = {launch, capture};
  return pair;
}

XDetection GoldenRefSimX::simulate_transition_fault(const CompiledSimGraph& cg,
                                                    const TestVector& v1,
                                                    const TestVector& v2,
                                                    const CompactFault& fault,
                                                    const XRunOptions& options) const {
  if (fault.exclusion != FaultExclusion::NONE) {
    return XDetection::NONE;
  }
  const TestVector pair = transition_pair(v1, v2);
  const std::vector<XSnapshot> good = simulate_sequence(cg, pair, nullptr, options);
  if (good.size() < 2) {
    return XDetection::NONE;
  }
  const int site = cg.compiled_to_yosys[fault.net_index];
  if (good[0].at(site) != transition_pre(fault.type) ||
      good[1].at(site) != transition_post(fault.type)) {
    return XDetection::NONE;
  }
  return classify_detection(cg, good, simulate_sequence(cg, pair, &fault, options));
}

}  // namespace faultflow
