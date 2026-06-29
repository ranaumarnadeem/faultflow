#include "atpg/compaction.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

#include "db/sqlite_store.hpp"
#include "fault/effect/compact_fault.hpp"
#include "fault/effect/fault_batch.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::atpg {
namespace {

struct Target {
  int64_t fault_id = 0;
  CompactFault fault;
};

TestVector build_vector(const ParsedGraph& parsed,
                        const std::map<std::string, bool>& values,
                        const std::vector<std::string>& input_order) {
  TestVector vec;
  for (const auto& name : input_order) {
    const auto it = values.find(name);
    // Unassigned PI = don't-care = 0, the value the applied test actually drives.
    // ATPG leaves PIs unobserved by the target fault unassigned (e.g. a core's irq
    // bus), and the compaction X-extraction probes by dropping PIs; both rely on
    // missing == 0 (matching the dense-cube fill and the stored pattern), so treat
    // it as 0 rather than aborting the whole campaign with "missing PI in vector".
    vec.inputs[parsed.net_id_by_name(name)] =
        (it != values.end()) ? it->second : false;
  }
  return vec;
}

// Mirrors make_batch in progressive_atpg.cpp: assign lanes 1..size (bit 0 is the
// fault-free baseline) and OR their masks into the batch mask.
FaultBatch make_batch(const std::vector<Target>& targets, size_t begin,
                      size_t end) {
  FaultBatch batch;
  batch.size = static_cast<int>(end - begin);
  for (size_t i = begin; i < end; ++i) {
    CompactFault lane = targets[i].fault;
    lane.bit = static_cast<uint8_t>((i - begin) + 1);
    lane.sa_mask = 1ULL << lane.bit;
    batch.faults[i - begin] = lane;
    batch.mask |= lane.sa_mask;
  }
  return batch;
}

}  // namespace

std::vector<int64_t> detect_with_vector_unfiltered(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_cached_graph(json_path, cell_map_path, unsupported_policy,
                        blackbox_instances);
  const TestVector tv = build_vector(ctx.parsed, vector, input_order);

  // One batched query (matches db::load_faults usage in progressive_atpg.cpp).
  const std::map<int64_t, db::FaultRecord> records =
      db::load_faults(db_path, fault_ids);
  std::vector<Target> targets;
  targets.reserve(fault_ids.size());
  for (int64_t fault_id : fault_ids) {
    const auto it = records.find(fault_id);
    if (it == records.end()) {
      throw std::runtime_error("fault not found: " + std::to_string(fault_id));
    }
    const db::FaultRecord& rec = it->second;
    // Status-agnostic on purpose: only skip faults that are not real simulation
    // targets (excluded or collapsed). The shared hot-path loader additionally
    // skips status != UNDETECTED, which would drop every detected fault here.
    if (rec.exclusion != FaultExclusion::NONE ||
        rec.collapsed_into != std::numeric_limits<uint32_t>::max()) {
      continue;
    }
    CompactFault fault;
    fault.net_index = rec.compiled_net_index;
    fault.type = rec.type;
    fault.status = rec.status;
    fault.exclusion = rec.exclusion;
    fault.collapsed_into = rec.collapsed_into;
    targets.push_back({fault_id, fault});
  }

  BitParallelSim sim;
  std::vector<int64_t> detected;
  const size_t lanes = static_cast<size_t>(kBatchSize);
  for (size_t begin = 0; begin < targets.size(); begin += lanes) {
    const size_t end = std::min(begin + lanes, targets.size());
    const FaultBatch batch = make_batch(targets, begin, end);
    const uint64_t detected_mask = sim.simulate_batch(ctx.cg, tv, batch);
    for (size_t i = begin; i < end; ++i) {
      const CompactFault& lane = batch.faults[i - begin];
      if ((detected_mask & lane.sa_mask) != 0) {
        detected.push_back(targets[i].fault_id);
      }
    }
  }
  return detected;
}

std::vector<int64_t> detect_with_pair_unfiltered(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_cached_graph(json_path, cell_map_path, unsupported_policy,
                        blackbox_instances);
  const TestVector v1 = build_vector(ctx.parsed, launch, input_order);
  const TestVector v2 = build_vector(ctx.parsed, capture, input_order);

  const std::map<int64_t, db::FaultRecord> records =
      db::load_faults(db_path, fault_ids);
  std::vector<Target> targets;
  targets.reserve(fault_ids.size());
  for (int64_t fault_id : fault_ids) {
    const auto it = records.find(fault_id);
    if (it == records.end()) {
      throw std::runtime_error("fault not found: " + std::to_string(fault_id));
    }
    const db::FaultRecord& rec = it->second;
    if (rec.exclusion != FaultExclusion::NONE ||
        rec.collapsed_into != std::numeric_limits<uint32_t>::max()) {
      continue;
    }
    CompactFault fault;
    fault.net_index = rec.compiled_net_index;
    fault.type = rec.type;
    fault.status = rec.status;
    fault.exclusion = rec.exclusion;
    fault.collapsed_into = rec.collapsed_into;
    fault.model = FaultModel::TRANSITION;
    targets.push_back({fault_id, fault});
  }

  BitParallelSim sim;
  std::vector<int64_t> detected;
  const size_t lanes = static_cast<size_t>(kBatchSize);
  for (size_t begin = 0; begin < targets.size(); begin += lanes) {
    const size_t end = std::min(begin + lanes, targets.size());
    const FaultBatch batch = make_batch(targets, begin, end);
    const uint64_t detected_mask =
        sim.simulate_transition_batch(ctx.cg, v1, v2, batch);
    for (size_t i = begin; i < end; ++i) {
      const CompactFault& lane = batch.faults[i - begin];
      if ((detected_mask & lane.sa_mask) != 0) {
        detected.push_back(targets[i].fault_id);
      }
    }
  }
  return detected;
}

}  // namespace faultflow::atpg
