#include "sim/xsim/sequence_grade_x.hpp"

#include <algorithm>
#include <set>
#include <stdexcept>

#include "common/threads.hpp"
#include "fault/effect/fault_batch.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/xsim/bit_parallel_sim_x.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

namespace faultflow {
namespace {

// grade_sequence_faults' vector, plus the per-cycle unknown inputs.
TestVector build_vector(const ParsedGraph& parsed, const CompiledSimGraph& cg,
                        const XSequenceGradeRequest& request, XRunOptions& options) {
  const SequenceGradeRequest& base = request.base;
  if (base.sample.size() != base.cycles.size()) {
    throw std::runtime_error("sequence grade: sample needs one flag per cycle");
  }
  if (!base.fault_active.empty() && base.fault_active.size() != base.cycles.size()) {
    throw std::runtime_error(
        "sequence grade: fault_active needs one flag per cycle (or none)");
  }
  if (!request.unknown.empty() && request.unknown.size() != base.cycles.size()) {
    throw std::runtime_error("sequence grade: unknown needs one row per cycle (or none)");
  }
  std::set<int> pi_yids;
  for (int pi : cg.pi_nets) {
    pi_yids.insert(cg.compiled_to_yosys[pi]);
  }
  std::vector<int> input_yids;
  for (const std::string& name : base.input_order) {
    const int yid = parsed.net_id_by_name(name);
    if (pi_yids.count(yid) == 0) {
      throw std::runtime_error("sequence grade: not a primary input: " + name);
    }
    input_yids.push_back(yid);
  }
  TestVector vec;
  for (size_t c = 0; c < base.cycles.size(); ++c) {
    const std::vector<bool>& values = base.cycles[c];
    if (values.size() != input_yids.size()) {
      throw std::runtime_error("sequence grade: cycle " + std::to_string(c) +
                               " does not give one value per input");
    }
    TestCycle cycle;
    cycle.sample_outputs = base.sample[c];
    cycle.fault_active = base.fault_active.empty() || base.fault_active[c];
    std::set<int> unknown;
    for (size_t i = 0; i < input_yids.size(); ++i) {
      cycle.inputs[input_yids[i]] = values[i];
      if (!request.unknown.empty()) {
        if (request.unknown[c].size() != input_yids.size()) {
          throw std::runtime_error("sequence grade: unknown row " + std::to_string(c) +
                                   " does not give one flag per input");
        }
        if (request.unknown[c][i]) {
          unknown.insert(input_yids[i]);
        }
      }
    }
    vec.cycles.push_back(std::move(cycle));
    options.unknown_inputs.push_back(std::move(unknown));
  }
  options.initial_state = request.initial_unknown ? Logic::X : Logic::ZERO;
  if (!request.initial_unknown && base.initial_ff_value) {
    for (uint32_t i = 0; i < static_cast<uint32_t>(cg.ff_nodes.size()); ++i) {
      vec.initial_ff_state[i] = true;
    }
  }
  return vec;
}

CompactFault make_fault(const SequenceFaultSpec& spec, const CompiledSimGraph& cg) {
  if (spec.compiled_net_index >= static_cast<uint32_t>(cg.net_count)) {
    throw std::runtime_error("sequence grade: fault net index out of range");
  }
  if (spec.fault_type > 1) {
    throw std::runtime_error("sequence grade: fault type must be 0 (SA0) or 1 (SA1)");
  }
  CompactFault fault;
  fault.net_index = spec.compiled_net_index;
  fault.type = spec.fault_type == 0 ? FaultType::SA0 : FaultType::SA1;
  fault.exclusion = FaultExclusion::NONE;
  return fault;
}

// The first sample of each kind, as detect_batch_observed reports for one lane.
std::pair<int32_t, int32_t> first_detections(const std::vector<std::vector<Logic>>& golden,
                                             const std::vector<XSnapshot>& faulty,
                                             const std::vector<int>& observe_yids) {
  int32_t definite = -1;
  int32_t possible = -1;
  for (size_t s = 0; s < std::min(golden.size(), faulty.size()); ++s) {
    for (size_t o = 0; o < observe_yids.size(); ++o) {
      const Logic g = golden[s][o];
      const Logic f = faulty[s].at(observe_yids[o]);
      if (!is_known(g)) {
        continue;
      }
      if (!is_known(f)) {
        possible = possible < 0 ? static_cast<int32_t>(s) : possible;
      } else if (f != g) {
        definite = definite < 0 ? static_cast<int32_t>(s) : definite;
      }
    }
  }
  return {definite, possible};
}

}  // namespace

XSequenceGradeResult grade_sequence_faults_x(
    const std::string& json_path, const std::string& cell_map_path,
    const XSequenceGradeRequest& request, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads, bool reference) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;
  XRunOptions options;
  const TestVector vec = build_vector(parsed, cg, request, options);

  std::vector<int> observe_yids;
  std::vector<uint32_t> observe;
  for (const std::string& name : request.base.observe_outputs) {
    const int yid = parsed.net_id_by_name(name);
    const auto it = cg.yosys_to_compiled.find(yid);
    if (it == cg.yosys_to_compiled.end()) {
      throw std::runtime_error("sequence grade: not a compiled net: " + name);
    }
    observe_yids.push_back(yid);
    observe.push_back(static_cast<uint32_t>(it->second));
  }

  const GoldenRefSimX ref;
  XSequenceGradeResult result;
  for (const XSnapshot& sample : ref.simulate_sequence(cg, vec, nullptr, options, &observe_yids)) {
    std::vector<Logic> row;
    for (int yid : observe_yids) {
      row.push_back(sample.at(yid));
    }
    result.golden.push_back(std::move(row));
  }

  const std::vector<SequenceFaultSpec>& specs = request.base.faults;
  const int fault_count = static_cast<int>(specs.size());
  result.first_definite.assign(specs.size(), -1);
  result.first_possible.assign(specs.size(), -1);
  std::vector<CompactFault> faults;
  for (const SequenceFaultSpec& spec : specs) {
    faults.push_back(make_fault(spec, cg));
  }
  const int threads = effective_sim_threads(sim_threads);

  if (reference) {
    parallel_ranges(fault_count, threads, [&](int i) {
      const auto faulty = ref.simulate_sequence(cg, vec, &faults[static_cast<size_t>(i)],
                                                options, &observe_yids);
      const auto [definite, possible] = first_detections(result.golden, faulty, observe_yids);
      result.first_definite[static_cast<size_t>(i)] = definite;
      result.first_possible[static_cast<size_t>(i)] = possible;
    });
    return result;
  }

  const int batch_count = (fault_count + kBatchSize - 1) / kBatchSize;
  parallel_ranges(batch_count, threads, [&](int b) {
    const size_t begin = static_cast<size_t>(b) * kBatchSize;
    const size_t end = std::min(begin + kBatchSize, faults.size());
    FaultBatch batch;
    batch.size = static_cast<int>(end - begin);
    for (size_t i = begin; i < end; ++i) {
      CompactFault fault = faults[i];
      const int lane = static_cast<int>(i - begin) + 1;
      fault.bit = static_cast<uint8_t>(lane);
      fault.sa_mask = 1ULL << lane;
      batch.faults[i - begin] = fault;
      batch.mask |= fault.sa_mask;
    }
    const BitParallelSimX sim;
    const auto observed = sim.detect_batch_observed(cg, vec, batch, observe, options);
    if (observed.lane0 != result.golden) {
      throw std::runtime_error(
          "sequence grade: the X engine's fault-free lane disagrees with the X "
          "reference simulator");
    }
    for (size_t i = begin; i < end; ++i) {
      result.first_definite[i] = observed.first_definite[i - begin + 1];
      result.first_possible[i] = observed.first_possible[i - begin + 1];
    }
  });
  return result;
}

}  // namespace faultflow
