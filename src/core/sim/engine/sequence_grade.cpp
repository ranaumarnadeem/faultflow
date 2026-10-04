#include "sim/engine/sequence_grade.hpp"

#include <algorithm>
#include <array>
#include <map>
#include <set>
#include <stdexcept>

#include "common/threads.hpp"
#include "fault/effect/compact_fault.hpp"
#include "fault/effect/fault_batch.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow {
namespace {

TestVector build_vector(const ParsedGraph& parsed, const CompiledSimGraph& cg,
                        const SequenceGradeRequest& request) {
  if (request.sample.size() != request.cycles.size()) {
    throw std::runtime_error("sequence grade: sample needs one flag per cycle");
  }
  if (!request.fault_active.empty() &&
      request.fault_active.size() != request.cycles.size()) {
    throw std::runtime_error(
        "sequence grade: fault_active needs one flag per cycle (or none)");
  }
  std::set<int> pi_yids;
  for (int pi : cg.pi_nets) {
    pi_yids.insert(cg.compiled_to_yosys[pi]);
  }
  std::vector<int> input_yids;
  input_yids.reserve(request.input_order.size());
  for (const std::string& name : request.input_order) {
    const int yid = parsed.net_id_by_name(name);
    if (pi_yids.count(yid) == 0) {
      throw std::runtime_error("sequence grade: not a primary input: " + name);
    }
    input_yids.push_back(yid);
  }
  TestVector vec;
  vec.cycles.reserve(request.cycles.size());
  for (size_t c = 0; c < request.cycles.size(); ++c) {
    const std::vector<bool>& values = request.cycles[c];
    if (values.size() != input_yids.size()) {
      throw std::runtime_error("sequence grade: cycle " + std::to_string(c) +
                               " does not give one value per input");
    }
    TestCycle cycle;
    cycle.sample_outputs = request.sample[c];
    cycle.fault_active = request.fault_active.empty() || request.fault_active[c];
    for (size_t i = 0; i < input_yids.size(); ++i) {
      cycle.inputs[input_yids[i]] = values[i];
    }
    vec.cycles.push_back(std::move(cycle));
  }
  if (request.initial_ff_value) {
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
  fault.bit = 1;
  fault.sa_mask = 1ULL << 1;
  return fault;
}

int32_t first_difference(const std::vector<std::vector<bool>>& golden,
                         const std::vector<std::map<int, bool>>& faulty,
                         const std::vector<int>& observe_yids) {
  const size_t count = std::min(golden.size(), faulty.size());
  for (size_t s = 0; s < count; ++s) {
    for (size_t o = 0; o < observe_yids.size(); ++o) {
      if (faulty[s].at(observe_yids[o]) != golden[s][o]) {
        return static_cast<int32_t>(s);
      }
    }
  }
  return -1;
}

}  // namespace

SequenceGradeResult grade_sequence_faults(
    const std::string& json_path, const std::string& cell_map_path,
    const SequenceGradeRequest& request, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads,
    bool reference) {
  const CachedGraph& graph = load_cached_graph(json_path, cell_map_path,
                                               unsupported_policy, blackbox_instances);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;
  const TestVector vec = build_vector(parsed, cg, request);

  std::vector<int> observe_yids;
  std::vector<uint32_t> observe;
  for (const std::string& name : request.observe_outputs) {
    const int yid = parsed.net_id_by_name(name);
    const auto it = cg.yosys_to_compiled.find(yid);
    if (it == cg.yosys_to_compiled.end()) {
      throw std::runtime_error("sequence grade: not a compiled net: " + name);
    }
    observe_yids.push_back(yid);
    observe.push_back(static_cast<uint32_t>(it->second));
  }

  const GoldenRefSim golden_sim;
  SequenceGradeResult result;
  for (const auto& sample :
       golden_sim.simulate_sequence_fault_free(cg, vec, observe_yids)) {
    std::vector<bool> row;
    row.reserve(observe_yids.size());
    for (int yid : observe_yids) {
      row.push_back(sample.at(yid));
    }
    result.golden.push_back(std::move(row));
  }

  const int fault_count = static_cast<int>(request.faults.size());
  result.first_sample.assign(request.faults.size(), -1);
  std::vector<CompactFault> faults;
  faults.reserve(request.faults.size());
  for (const SequenceFaultSpec& spec : request.faults) {
    faults.push_back(make_fault(spec, cg));
  }
  const int threads = effective_sim_threads(sim_threads);

  if (reference) {
    parallel_ranges(fault_count, threads, [&](int i) {
      const auto faulty = golden_sim.simulate_sequence_with_fault(
          cg, vec, faults[static_cast<size_t>(i)], observe_yids);
      result.first_sample[static_cast<size_t>(i)] =
          first_difference(result.golden, faulty, observe_yids);
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
    std::array<int32_t, 64> first{};
    std::vector<std::vector<bool>> lane0;
    const BitParallelSim sim;
    sim.simulate_batch_observed(cg, vec, batch, observe, first, true, &lane0);
    for (size_t s = 0; s < lane0.size(); ++s) {
      if (lane0[s] != result.golden.at(s)) {
        throw std::runtime_error(
            "sequence grade: the bit-parallel fault-free lane disagrees with the "
            "reference simulator at sample " +
            std::to_string(s));
      }
    }
    for (size_t i = begin; i < end; ++i) {
      result.first_sample[i] = first[i - begin + 1];
    }
  });
  return result;
}

}  // namespace faultflow
