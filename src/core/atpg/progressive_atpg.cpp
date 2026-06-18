#include "atpg/progressive_atpg.hpp"

#include <algorithm>
#include <random>
#include <stdexcept>
#include <utility>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "db/sqlite_store.hpp"
#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::atpg {
namespace {

constexpr size_t kFaultLanesPerWord = kBatchSize;

SimulationInstrumentation g_simulation_instrumentation;

// Thin wrapper over the shared, content-keyed graph cache: the netlist never
// changes during a run, so the graph is built once and every solve/sim call
// reuses the same immutable instance. Returns a reference into the cache (no
// per-call copy); callers read it read-only.
const CachedGraph& load_graph(const std::string& json_path,
                              const std::string& cell_map_path,
                              const std::string& unsupported_policy,
                              bool require_combinational = true) {
  const CachedGraph& ctx =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  if (require_combinational && !ctx.cg.ff_nodes.empty()) {
    throw std::runtime_error("progressive native ATPG is combinational-only");
  }
  return ctx;
}

// Derives the scan LOC coupling from a reduced ATPG view: each `__ppi_<inst>`
// input port (a scan FF's current state) is coupled to its `__ppo_<inst>` output
// port (the FF's next-state); every other PI is a held real PI.
struct ScanLocView {
  std::vector<LocCouple> couples;
  std::vector<uint32_t> held_pis;
};

ScanLocView build_scan_loc_view(const ParsedGraph& parsed,
                                const CompiledSimGraph& cg) {
  const std::string ppi_prefix = "__ppi_";
  const std::string ppo_prefix = "__ppo_";
  const ParsedModule& mod = parsed.top_module();
  ScanLocView view;
  for (const auto& [name, port] : mod.ports) {
    if (port.direction != "input" || port.bits.size() != 1) {
      continue;
    }
    const auto cit = cg.yosys_to_compiled.find(port.bits.front());
    if (cit == cg.yosys_to_compiled.end()) {
      continue;
    }
    const uint32_t ppi_compiled = static_cast<uint32_t>(cit->second);
    if (name.rfind(ppi_prefix, 0) != 0) {
      view.held_pis.push_back(ppi_compiled);
      continue;
    }
    const std::string ppo_name = ppo_prefix + name.substr(ppi_prefix.size());
    const auto pit = mod.ports.find(ppo_name);
    if (pit == mod.ports.end() || pit->second.bits.size() != 1) {
      throw std::runtime_error("scan LOC view: PPI without matching PPO: " +
                               name);
    }
    const auto poit = cg.yosys_to_compiled.find(pit->second.bits.front());
    if (poit == cg.yosys_to_compiled.end()) {
      throw std::runtime_error("scan LOC view: PPO net not compiled: " +
                               ppo_name);
    }
    view.couples.push_back(
        {ppi_compiled, static_cast<uint32_t>(poit->second)});
  }
  return view;
}

std::vector<CompactFault> enumerate_all(
    const NormalizedGraph& ng, const CompiledSimGraph& cg, bool include_clock_faults,
    bool include_reset_faults, bool collapsing) {
  EnumeratorOptions options;
  options.include_clock_faults = include_clock_faults;
  options.include_reset_faults = include_reset_faults;
  std::vector<CompactFault> faults = enumerate_faults(ng, cg, options);
  if (collapsing) {
    faults = collapse_primitive_faults(ng, cg, std::move(faults));
  }
  for (auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE ||
        fault.collapsed_into != UINT32_MAX) {
      fault.status = FaultStatus::UNDETECTED;
    } else {
      fault.status = FaultStatus::UNDETECTED;
    }
  }
  return faults;
}

CompactFault fault_from_record(const db::FaultRecord& rec) {
  CompactFault fault;
  fault.net_index = rec.compiled_net_index;
  fault.type = rec.type;
  fault.status = rec.status;
  fault.exclusion = rec.exclusion;
  fault.collapsed_into = rec.collapsed_into;
  return fault;
}

struct ActiveFaultRecord {
  int64_t fault_id = 0;
  CompactFault fault;
};

std::vector<ActiveFaultRecord> load_active_fault_records(
    const std::string& db_path, const std::vector<int64_t>& fault_ids,
    bool skip_protocol_unresolved) {
  // One batched query instead of a fresh connection per fault. Drive the loop
  // by the original fault_ids (not the map) so `active` keeps its exact prior
  // ordering, and throw on a missing id exactly as db::load_fault did.
  const std::map<int64_t, db::FaultRecord> records =
      db::load_faults(db_path, fault_ids);
  std::vector<ActiveFaultRecord> active;
  active.reserve(fault_ids.size());
  for (int64_t fault_id : fault_ids) {
    const auto it = records.find(fault_id);
    if (it == records.end()) {
      throw std::runtime_error("fault not found: " + std::to_string(fault_id));
    }
    const db::FaultRecord& rec = it->second;
    if (rec.exclusion != FaultExclusion::NONE || rec.collapsed_into != UINT32_MAX ||
        rec.status != FaultStatus::UNDETECTED ||
        (skip_protocol_unresolved && rec.protocol_unresolved)) {
      continue;
    }
    active.push_back({fault_id, fault_from_record(rec)});
  }
  return active;
}

FaultBatch make_batch(const std::vector<ActiveFaultRecord>& active, size_t begin,
                      size_t end) {
  FaultBatch batch;
  batch.size = static_cast<int>(end - begin);
  for (size_t i = begin; i < end; ++i) {
    CompactFault lane = active[i].fault;
    lane.bit = static_cast<uint8_t>((i - begin) + 1);
    lane.sa_mask = 1ULL << lane.bit;
    batch.faults[i - begin] = lane;
    batch.mask |= lane.sa_mask;
  }
  return batch;
}

TestVector vector_from_map(const ParsedGraph& parsed,
                           const std::map<std::string, bool>& values,
                           const std::vector<std::string>& input_order) {
  TestVector vec;
  for (const auto& input : input_order) {
    const auto it = values.find(input);
    if (it == values.end()) {
      throw std::runtime_error("missing PI in vector: " + input);
    }
    vec.inputs[parsed.net_id_by_name(input)] = it->second;
  }
  return vec;
}

std::string solve_result_name(SatSolveResult result) {
  switch (result) {
    case SatSolveResult::SAT:
      return "SAT";
    case SatSolveResult::UNSAT:
      return "UNSAT";
    case SatSolveResult::TIMEOUT:
      return "TIMEOUT";
    case SatSolveResult::UNKNOWN:
      return "UNKNOWN";
  }
  return "UNKNOWN";
}

}  // namespace

void reset_simulation_instrumentation() {
  g_simulation_instrumentation = {};
  // Drop cached graphs too so a reset gives a deterministic cold start: the
  // next load is a guaranteed build, keeping load_graph_calls meaningful.
  clear_graph_cache();
}

SimulationInstrumentation simulation_instrumentation() {
  SimulationInstrumentation stats = g_simulation_instrumentation;
  // load_graph_calls now means "graphs actually built"; the cache owns that
  // count (it is the only thing that builds graphs).
  stats.load_graph_calls = graph_cache_stats().builds;
  return stats;
}

std::vector<std::map<std::string, bool>> generate_random_vectors(
    const std::vector<std::string>& input_order, int count, uint64_t seed) {
  std::vector<std::map<std::string, bool>> vectors;
  std::mt19937_64 rng(seed);
  vectors.reserve(static_cast<size_t>(count));
  for (int i = 0; i < count; ++i) {
    std::map<std::string, bool> vector;
    for (const auto& input : input_order) {
      vector[input] = (rng() & 1ULL) != 0;
    }
    vectors.push_back(std::move(vector));
  }
  return vectors;
}

void ensure_faults_enumerated(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, bool include_clock_faults,
    bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy) {
  db::init_database(db_path);
  if (db::fault_count(db_path, campaign_id) > 0) {
    return;
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, false);
  const std::vector<CompactFault> faults =
      enumerate_all(ctx.ng, ctx.cg, include_clock_faults, include_reset_faults,
                    collapsing);
  db::insert_faults_if_empty(db_path, campaign_id, ctx.ng, ctx.cg, faults);
}

SolveFaultResult solve_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  if (rec.exclusion != FaultExclusion::NONE || rec.collapsed_into != UINT32_MAX ||
      rec.status != FaultStatus::UNDETECTED) {
    throw std::runtime_error("fault is not active for SAT ATPG");
  }
  const auto pis = ordered_pis(ctx.parsed, ctx.cg);
  CompactFault fault = fault_from_record(rec);

  SatSolveOptions options;
  options.conflict_limit = conflict_limit;
  options.sat_timeout_seconds = sat_timeout_seconds;
  options.blocked_patterns = blocked_patterns;

  std::map<std::string, bool> vector;
  const SatSolveResult result =
      solve_stuck_at_fault(ctx.cg, pis, fault, options, vector);

  SolveFaultResult out;
  out.result = solve_result_name(result);
  if (result == SatSolveResult::SAT) {
    out.vector = std::move(vector);
  }
  return out;
}

bool verify_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  CompactFault fault = fault_from_record(rec);
  GoldenRefSim golden;
  const TestVector vec = [&]() {
    TestVector tv;
    for (const auto& [name, value] : vector) {
      tv.inputs[ctx.parsed.net_id_by_name(name)] = value;
    }
    return tv;
  }();
  return golden.is_detected(ctx.cg, golden.simulate_fault_free(ctx.cg, vec),
                            golden.simulate_with_fault(ctx.cg, vec, fault));
}

std::vector<ProgressiveDetection> simulate_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy) {
  if (new_vectors.empty() || fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  std::vector<TestVector> vectors;
  vectors.reserve(new_vectors.size());
  for (const auto& raw : new_vectors) {
    vectors.push_back(vector_from_map(ctx.parsed, raw, input_order));
  }

  BitParallelSim sim;
  std::vector<ProgressiveDetection> detections;
  std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, false);
  for (size_t vi = 0; vi < vectors.size() && !active.empty(); ++vi) {
    std::vector<ActiveFaultRecord> still_active;
    still_active.reserve(active.size());
    for (size_t begin = 0; begin < active.size(); begin += kFaultLanesPerWord) {
      const size_t end =
          std::min(begin + kFaultLanesPerWord, active.size());
      const FaultBatch batch = make_batch(active, begin, end);
      ++g_simulation_instrumentation.batch_fault_calls;
      const uint64_t detected_mask =
          sim.simulate_batch(ctx.cg, vectors[vi], batch);
      for (size_t i = begin; i < end; ++i) {
        const CompactFault& lane = batch.faults[i - begin];
        if ((detected_mask & lane.sa_mask) != 0) {
          const int64_t vector_index =
              vector_start_index + static_cast<int64_t>(vi);
          db::mark_fault_detected(db_path, campaign_id, run_id,
                                  active[i].fault_id, vector_index);
          detections.push_back({active[i].fault_id, vector_index});
        } else {
          still_active.push_back(active[i]);
        }
      }
    }
    active = std::move(still_active);
  }
  return detections;
}

std::vector<int64_t> simulate_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const TestVector tv = vector_from_map(ctx.parsed, vector, input_order);
  BitParallelSim sim;
  std::vector<int64_t> detected;
  const std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, true);
  for (size_t begin = 0; begin < active.size(); begin += kFaultLanesPerWord) {
    const size_t end = std::min(begin + kFaultLanesPerWord, active.size());
    const FaultBatch batch = make_batch(active, begin, end);
    ++g_simulation_instrumentation.batch_fault_calls;
    const uint64_t detected_mask = sim.simulate_batch(ctx.cg, tv, batch);
    for (size_t i = begin; i < end; ++i) {
      const CompactFault& lane = batch.faults[i - begin];
      if ((detected_mask & lane.sa_mask) != 0) {
        detected.push_back(active[i].fault_id);
      }
    }
  }
  return detected;
}

// ---- Transition model (combinational broadside two-pattern) ----------------

std::vector<VectorPair> generate_random_vector_pairs(
    const std::vector<std::string>& input_order, int count, uint64_t seed) {
  std::vector<VectorPair> pairs;
  std::mt19937_64 rng(seed);
  pairs.reserve(static_cast<size_t>(count));
  const auto draw = [&]() {
    std::map<std::string, bool> vector;
    for (const auto& input : input_order) {
      vector[input] = (rng() & 1ULL) != 0;
    }
    return vector;
  };
  for (int i = 0; i < count; ++i) {
    std::map<std::string, bool> launch = draw();
    std::map<std::string, bool> capture = draw();
    pairs.emplace_back(std::move(launch), std::move(capture));
  }
  return pairs;
}

SolveTransitionResult solve_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  if (rec.exclusion != FaultExclusion::NONE || rec.collapsed_into != UINT32_MAX ||
      rec.status != FaultStatus::UNDETECTED) {
    throw std::runtime_error("fault is not active for transition SAT ATPG");
  }
  const auto pis = ordered_pis(ctx.parsed, ctx.cg);
  CompactFault fault = fault_from_record(rec);
  fault.model = FaultModel::TRANSITION;

  SatSolveOptions options;
  options.conflict_limit = conflict_limit;
  options.sat_timeout_seconds = sat_timeout_seconds;
  options.blocked_patterns = blocked_patterns;

  std::map<std::string, bool> launch;
  std::map<std::string, bool> capture;
  const SatSolveResult result =
      solve_transition_fault(ctx.cg, pis, fault, options, launch, capture);

  SolveTransitionResult out;
  out.result = solve_result_name(result);
  if (result == SatSolveResult::SAT) {
    out.launch = std::move(launch);
    out.capture = std::move(capture);
  }
  return out;
}

SolveTransitionResult solve_scan_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  if (rec.exclusion != FaultExclusion::NONE || rec.collapsed_into != UINT32_MAX ||
      rec.status != FaultStatus::UNDETECTED) {
    throw std::runtime_error("fault is not active for transition SAT ATPG");
  }
  const auto pis = ordered_pis(ctx.parsed, ctx.cg);
  const ScanLocView view = build_scan_loc_view(ctx.parsed, ctx.cg);
  CompactFault fault = fault_from_record(rec);
  fault.model = FaultModel::TRANSITION;

  SatSolveOptions options;
  options.conflict_limit = conflict_limit;
  options.sat_timeout_seconds = sat_timeout_seconds;
  options.blocked_patterns = blocked_patterns;

  std::map<std::string, bool> launch;
  std::map<std::string, bool> capture;
  const SatSolveResult result = solve_scan_transition_fault(
      ctx.cg, pis, view.couples, view.held_pis, fault, options, launch, capture);

  SolveTransitionResult out;
  out.result = solve_result_name(result);
  if (result == SatSolveResult::SAT) {
    out.launch = std::move(launch);
    out.capture = std::move(capture);
  }
  return out;
}

SolveTransitionResult solve_scan_los_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::pair<std::string, std::string>>& couple_ports,
    const std::vector<std::string>& head_ppi_ports,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  if (rec.exclusion != FaultExclusion::NONE || rec.collapsed_into != UINT32_MAX ||
      rec.status != FaultStatus::UNDETECTED) {
    throw std::runtime_error("fault is not active for transition SAT ATPG");
  }
  const auto pis = ordered_pis(ctx.parsed, ctx.cg);
  const ParsedModule& mod = ctx.parsed.top_module();
  const auto port_compiled = [&](const std::string& port) -> uint32_t {
    const auto pit = mod.ports.find(port);
    if (pit == mod.ports.end() || pit->second.bits.size() != 1) {
      throw std::runtime_error("scan LOS view: bad port " + port);
    }
    const auto cit = ctx.cg.yosys_to_compiled.find(pit->second.bits.front());
    if (cit == ctx.cg.yosys_to_compiled.end()) {
      throw std::runtime_error("scan LOS view: net not compiled for " + port);
    }
    return static_cast<uint32_t>(cit->second);
  };

  std::vector<LosCouple> couples;
  couples.reserve(couple_ports.size());
  for (const auto& [cap, pred] : couple_ports) {
    couples.push_back({port_compiled(cap), port_compiled(pred)});
  }
  std::vector<uint32_t> head_ppi;
  head_ppi.reserve(head_ppi_ports.size());
  for (const std::string& h : head_ppi_ports) {
    head_ppi.push_back(port_compiled(h));
  }
  // Held real PIs = every single-bit input port that is not a pseudo-PI.
  std::vector<uint32_t> held;
  for (const auto& [name, port] : mod.ports) {
    if (port.direction != "input" || port.bits.size() != 1) {
      continue;
    }
    if (name.rfind("__ppi_", 0) == 0) {
      continue;
    }
    const auto cit = ctx.cg.yosys_to_compiled.find(port.bits.front());
    if (cit != ctx.cg.yosys_to_compiled.end()) {
      held.push_back(static_cast<uint32_t>(cit->second));
    }
  }

  CompactFault fault = fault_from_record(rec);
  fault.model = FaultModel::TRANSITION;

  SatSolveOptions options;
  options.conflict_limit = conflict_limit;
  options.sat_timeout_seconds = sat_timeout_seconds;
  options.blocked_patterns = blocked_patterns;

  std::map<std::string, bool> launch;
  std::map<std::string, bool> capture;
  const SatSolveResult result = solve_scan_los_transition_fault(
      ctx.cg, pis, couples, head_ppi, held, fault, options, launch, capture);

  SolveTransitionResult out;
  out.result = solve_result_name(result);
  if (result == SatSolveResult::SAT) {
    out.launch = std::move(launch);
    out.capture = std::move(capture);
  }
  return out;
}

bool verify_transition_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::string& unsupported_policy) {
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const db::FaultRecord rec = db::load_fault(db_path, fault_id);
  CompactFault fault = fault_from_record(rec);
  fault.model = FaultModel::TRANSITION;
  const auto to_vec = [&](const std::map<std::string, bool>& values) {
    TestVector tv;
    for (const auto& [name, value] : values) {
      tv.inputs[ctx.parsed.net_id_by_name(name)] = value;
    }
    return tv;
  };
  GoldenRefSim golden;
  return golden.simulate_transition_fault(ctx.cg, to_vec(launch), to_vec(capture),
                                          fault);
}

std::vector<ProgressiveDetection> simulate_transition_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<VectorPair>& new_pairs,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy) {
  if (new_pairs.empty() || fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  std::vector<std::pair<TestVector, TestVector>> pairs;
  pairs.reserve(new_pairs.size());
  for (const auto& [launch, capture] : new_pairs) {
    pairs.emplace_back(vector_from_map(ctx.parsed, launch, input_order),
                       vector_from_map(ctx.parsed, capture, input_order));
  }

  BitParallelSim sim;
  std::vector<ProgressiveDetection> detections;
  std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, false);
  for (size_t vi = 0; vi < pairs.size() && !active.empty(); ++vi) {
    std::vector<ActiveFaultRecord> still_active;
    still_active.reserve(active.size());
    for (size_t begin = 0; begin < active.size(); begin += kFaultLanesPerWord) {
      const size_t end = std::min(begin + kFaultLanesPerWord, active.size());
      const FaultBatch batch = make_batch(active, begin, end);
      ++g_simulation_instrumentation.batch_fault_calls;
      const uint64_t detected_mask = sim.simulate_transition_batch(
          ctx.cg, pairs[vi].first, pairs[vi].second, batch);
      for (size_t i = begin; i < end; ++i) {
        const CompactFault& lane = batch.faults[i - begin];
        if ((detected_mask & lane.sa_mask) != 0) {
          const int64_t vector_index =
              vector_start_index + static_cast<int64_t>(vi);
          db::mark_fault_detected(db_path, campaign_id, run_id,
                                  active[i].fault_id, vector_index);
          detections.push_back({active[i].fault_id, vector_index});
        } else {
          still_active.push_back(active[i]);
        }
      }
    }
    active = std::move(still_active);
  }
  return detections;
}

std::vector<int64_t> simulate_transition_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx = load_graph(json_path, cell_map_path, unsupported_policy);
  const TestVector v1 = vector_from_map(ctx.parsed, launch, input_order);
  const TestVector v2 = vector_from_map(ctx.parsed, capture, input_order);
  BitParallelSim sim;
  std::vector<int64_t> detected;
  const std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, true);
  for (size_t begin = 0; begin < active.size(); begin += kFaultLanesPerWord) {
    const size_t end = std::min(begin + kFaultLanesPerWord, active.size());
    const FaultBatch batch = make_batch(active, begin, end);
    ++g_simulation_instrumentation.batch_fault_calls;
    const uint64_t detected_mask =
        sim.simulate_transition_batch(ctx.cg, v1, v2, batch);
    for (size_t i = begin; i < end; ++i) {
      const CompactFault& lane = batch.faults[i - begin];
      if ((detected_mask & lane.sa_mask) != 0) {
        detected.push_back(active[i].fault_id);
      }
    }
  }
  return detected;
}

}  // namespace faultflow::atpg
