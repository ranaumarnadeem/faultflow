#include "atpg/progressive_atpg.hpp"

#include <algorithm>
#include <exception>
#include <random>
#include <stdexcept>
#include <thread>
#include <tuple>
#include <utility>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "common/types.hpp"
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
                              const std::vector<std::string>& blackbox_instances,
                              bool require_combinational = true) {
  const CachedGraph& ctx =
      load_cached_graph(json_path, cell_map_path, unsupported_policy,
                        blackbox_instances);
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

// ---- Parallel fault grading -------------------------------------------------
// Grading the active fault list against one vector is embarrassingly parallel:
// the CompiledSimGraph is immutable and shared read-only, BitParallelSim is
// stateless (every simulate_batch allocates its own stack-local SimState), and a
// fault's detected/not-detected verdict is a pure function of (graph, vector,
// batch). So we slice the active list on 63-fault batch boundaries, grade each
// slice (optionally on its own thread), and merge per-slice results back in
// slice order. DB writes and the instrumentation-counter merge stay on the
// CALLING thread AFTER join, so the result is bit-identical to serial for any
// thread count and there is never more than one SQLite writer (never-happen #18).

struct GradeRangeResult {
  std::vector<size_t> detected_indices;         // indices into `active`, ascending
  std::vector<ActiveFaultRecord> still_active;  // carry-forward, ascending
  int64_t batch_fault_calls = 0;                // thread-local instrumentation tally
};

// Resolve the requested thread count. <= 0 means "auto" (hardware concurrency);
// the Python layer normally resolves this already, so this is just a safe floor.
int effective_sim_threads(int sim_threads) {
  if (sim_threads > 0) {
    return sim_threads;
  }
  const unsigned hw = std::thread::hardware_concurrency();
  return hw == 0 ? 1 : static_cast<int>(hw);
}

// Grade active[range_begin, range_end) — a whole number of 63-fault batches (the
// final partial batch lands in the last non-empty slice) — by calling
// grade(batch) for each batch. Pure compute: no DB, no globals, no Python.
template <typename GradeFn>
GradeRangeResult grade_batch_range(const std::vector<ActiveFaultRecord>& active,
                                   size_t range_begin, size_t range_end,
                                   const GradeFn& grade) {
  GradeRangeResult r;
  r.detected_indices.reserve(range_end - range_begin);
  r.still_active.reserve(range_end - range_begin);
  for (size_t begin = range_begin; begin < range_end;
       begin += kFaultLanesPerWord) {
    const size_t end = std::min(begin + kFaultLanesPerWord, range_end);
    const FaultBatch batch = make_batch(active, begin, end);
    ++r.batch_fault_calls;
    const uint64_t detected_mask = grade(batch);
    for (size_t i = begin; i < end; ++i) {
      const CompactFault& lane = batch.faults[i - begin];
      if ((detected_mask & lane.sa_mask) != 0) {
        r.detected_indices.push_back(i);
      } else {
        r.still_active.push_back(active[i]);
      }
    }
  }
  return r;
}

// Partition `active` into up to `sim_threads` slices on 63-fault batch
// boundaries, grade each slice (spawning sim_threads-1 workers and running one
// slice on the caller), and return per-slice results in slice order (== ascending
// fault-index order, so the merge reproduces serial output exactly). `grade` MUST
// be safe to call concurrently — BitParallelSim is stateless, so one shared
// instance qualifies. A slice that throws is captured; after every thread is
// joined the lowest-index failure is rethrown, so the surfaced exception is
// deterministic regardless of scheduling.
template <typename GradeFn>
std::vector<GradeRangeResult> grade_active_parallel(
    const std::vector<ActiveFaultRecord>& active, int sim_threads,
    const GradeFn& grade) {
  const size_t n = active.size();
  const size_t num_batches = (n + kFaultLanesPerWord - 1) / kFaultLanesPerWord;
  const size_t threads =
      num_batches == 0
          ? 1
          : std::min(static_cast<size_t>(effective_sim_threads(sim_threads)),
                     num_batches);

  std::vector<GradeRangeResult> results(threads);
  if (threads <= 1) {
    results[0] = grade_batch_range(active, 0, n, grade);
    return results;
  }

  const size_t base = num_batches / threads;
  const size_t rem = num_batches % threads;
  const auto bounds = [&](size_t t) {
    const size_t lo_batch = t * base + std::min(t, rem);
    const size_t hi_batch = lo_batch + base + (t < rem ? 1 : 0);
    return std::pair<size_t, size_t>{
        lo_batch * kFaultLanesPerWord,
        std::min(hi_batch * kFaultLanesPerWord, n)};
  };

  std::vector<std::thread> pool;
  pool.reserve(threads - 1);
  std::vector<std::exception_ptr> errors(threads);
  const auto run_slice = [&](size_t t) {
    try {
      const std::pair<size_t, size_t> range = bounds(t);
      results[t] = grade_batch_range(active, range.first, range.second, grade);
    } catch (...) {
      errors[t] = std::current_exception();
    }
  };

  for (size_t t = 1; t < threads; ++t) {
    pool.emplace_back(run_slice, t);
  }
  run_slice(0);
  for (std::thread& th : pool) {
    th.join();
  }
  for (size_t t = 0; t < threads; ++t) {
    if (errors[t]) {
      std::rethrow_exception(errors[t]);
    }
  }
  return results;
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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  db::init_database(db_path);
  if (db::fault_count(db_path, campaign_id) > 0) {
    return;
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy,
                 blackbox_instances, false);
  const std::vector<CompactFault> faults =
      enumerate_all(ctx.ng, ctx.cg, include_clock_faults, include_reset_faults,
                    collapsing);
  db::insert_faults_if_empty(db_path, campaign_id, ctx.ng, ctx.cg, faults);
}

SolveFaultResult solve_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode, bool cone_restrict) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
  const TestMode mode = parse_test_mode(test_mode);
  // Cone restriction applies to both the FUNCTIONAL and the mode-aware
  // (INTEST/EXTEST) stuck-at paths; the mode-aware path keeps the safe-zero WBR
  // forcing inside the cone.
  options.cone_restrict = cone_restrict;
  SatSolveResult result;
  if (mode == TestMode::FUNCTIONAL) {
    result = solve_stuck_at_fault(ctx.cg, pis, fault, options, vector);
  } else {
    const ModeConfig mc = build_mode_config(ctx.cg, mode);
    result = solve_stuck_at_fault(ctx.cg, pis, fault, options, vector, mc);
  }

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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
  const TestMode mode = parse_test_mode(test_mode);
  if (mode == TestMode::FUNCTIONAL || ctx.cg.wrapper_cells.empty()) {
    return golden.is_detected(ctx.cg, golden.simulate_fault_free(ctx.cg, vec),
                              golden.simulate_with_fault(ctx.cg, vec, fault));
  }
  const ModeConfig mc = build_mode_config(ctx.cg, mode);
  return golden.is_detected(ctx.cg,
                             golden.simulate_fault_free(ctx.cg, vec, mc),
                             golden.simulate_with_fault(ctx.cg, vec, fault, mc),
                             mc);
}

std::vector<ProgressiveDetection> simulate_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode, int sim_threads) {
  if (new_vectors.empty() || fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  std::vector<TestVector> vectors;
  vectors.reserve(new_vectors.size());
  for (const auto& raw : new_vectors) {
    vectors.push_back(vector_from_map(ctx.parsed, raw, input_order));
  }

  const TestMode mode = parse_test_mode(test_mode);
  const bool use_mode = (mode != TestMode::FUNCTIONAL) && !ctx.cg.wrapper_cells.empty();
  const ModeConfig mc = use_mode ? build_mode_config(ctx.cg, mode) : ModeConfig{};

  BitParallelSim sim;  // stateless; shared by every slice/thread for a vector
  std::vector<ProgressiveDetection> detections;
  std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, false);
  for (size_t vi = 0; vi < vectors.size() && !active.empty(); ++vi) {
    const TestVector& vec = vectors[vi];
    const auto grade = [&](const FaultBatch& batch) -> uint64_t {
      return use_mode ? sim.simulate_batch(ctx.cg, vec, batch, mc)
                      : sim.simulate_batch(ctx.cg, vec, batch);
    };
    const std::vector<GradeRangeResult> slices =
        grade_active_parallel(active, sim_threads, grade);

    // Merge on the calling thread in slice order (== ascending fault index):
    // this reproduces the serial detections/still_active order exactly, keeps
    // the single SQLite writer, and folds the per-thread instrumentation tally.
    std::vector<ActiveFaultRecord> still_active;
    still_active.reserve(active.size());
    const int64_t vector_index = vector_start_index + static_cast<int64_t>(vi);
    for (const GradeRangeResult& sr : slices) {
      for (const size_t idx : sr.detected_indices) {
        db::mark_fault_detected(db_path, campaign_id, run_id,
                                active[idx].fault_id, vector_index);
        detections.push_back({active[idx].fault_id, vector_index});
      }
      for (const ActiveFaultRecord& rec : sr.still_active) {
        still_active.push_back(rec);
      }
      g_simulation_instrumentation.batch_fault_calls += sr.batch_fault_calls;
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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const TestVector tv = vector_from_map(ctx.parsed, vector, input_order);
  const TestMode mode = parse_test_mode(test_mode);
  const bool use_mode = (mode != TestMode::FUNCTIONAL) && !ctx.cg.wrapper_cells.empty();
  const ModeConfig mc = use_mode ? build_mode_config(ctx.cg, mode) : ModeConfig{};
  BitParallelSim sim;
  std::vector<int64_t> detected;
  const std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, true);
  for (size_t begin = 0; begin < active.size(); begin += kFaultLanesPerWord) {
    const size_t end = std::min(begin + kFaultLanesPerWord, active.size());
    const FaultBatch batch = make_batch(active, begin, end);
    ++g_simulation_instrumentation.batch_fault_calls;
    const uint64_t detected_mask = use_mode
        ? sim.simulate_batch(ctx.cg, tv, batch, mc)
        : sim.simulate_batch(ctx.cg, tv, batch);
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
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, bool cone_restrict) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
  // Broadside two-frame transition: cone restricts the two capture machines;
  // the launch frame stays full. Scan LOC/LOS transition stays whole-circuit.
  options.cone_restrict = cone_restrict;

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
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, bool cone_restrict) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
  // Scan LOC transition: cone restricts the two capture machines; launch frame
  // full (the PPI<->PPO coupling needs every coupled PPO computed).
  options.cone_restrict = cone_restrict;

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
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, bool cone_restrict) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
  // Scan LOS transition: cone restricts the two capture machines; launch full.
  options.cone_restrict = cone_restrict;

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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads) {
  if (new_pairs.empty() || fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  std::vector<std::pair<TestVector, TestVector>> pairs;
  pairs.reserve(new_pairs.size());
  for (const auto& [launch, capture] : new_pairs) {
    pairs.emplace_back(vector_from_map(ctx.parsed, launch, input_order),
                       vector_from_map(ctx.parsed, capture, input_order));
  }

  BitParallelSim sim;  // stateless; shared by every slice/thread for a pair
  std::vector<ProgressiveDetection> detections;
  std::vector<ActiveFaultRecord> active =
      load_active_fault_records(db_path, fault_ids, false);
  for (size_t vi = 0; vi < pairs.size() && !active.empty(); ++vi) {
    const TestVector& launch = pairs[vi].first;
    const TestVector& capture = pairs[vi].second;
    const auto grade = [&](const FaultBatch& batch) -> uint64_t {
      return sim.simulate_transition_batch(ctx.cg, launch, capture, batch);
    };
    const std::vector<GradeRangeResult> slices =
        grade_active_parallel(active, sim_threads, grade);

    std::vector<ActiveFaultRecord> still_active;
    still_active.reserve(active.size());
    const int64_t vector_index = vector_start_index + static_cast<int64_t>(vi);
    for (const GradeRangeResult& sr : slices) {
      for (const size_t idx : sr.detected_indices) {
        db::mark_fault_detected(db_path, campaign_id, run_id,
                                active[idx].fault_id, vector_index);
        detections.push_back({active[idx].fault_id, vector_index});
      }
      for (const ActiveFaultRecord& rec : sr.still_active) {
        still_active.push_back(rec);
      }
      g_simulation_instrumentation.batch_fault_calls += sr.batch_fault_calls;
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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  if (fault_ids.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
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

std::vector<int64_t> simulate_tentative_from_preloaded(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::tuple<int64_t, uint32_t, uint8_t>>& preloaded,
    const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode, int sim_threads) {
  if (preloaded.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const TestVector tv = vector_from_map(ctx.parsed, vector, input_order);
  const TestMode mode = parse_test_mode(test_mode);
  const bool use_mode = (mode != TestMode::FUNCTIONAL) && !ctx.cg.wrapper_cells.empty();
  const ModeConfig mc = use_mode ? build_mode_config(ctx.cg, mode) : ModeConfig{};
  BitParallelSim sim;  // stateless; shared by every slice/thread

  std::vector<ActiveFaultRecord> active;
  active.reserve(preloaded.size());
  for (const auto& [fault_id, net_index, type] : preloaded) {
    CompactFault cf;
    cf.net_index = net_index;
    cf.type = static_cast<FaultType>(type);
    active.push_back({fault_id, cf});
  }

  const auto grade = [&](const FaultBatch& batch) -> uint64_t {
    return use_mode ? sim.simulate_batch(ctx.cg, tv, batch, mc)
                    : sim.simulate_batch(ctx.cg, tv, batch);
  };
  const std::vector<GradeRangeResult> slices =
      grade_active_parallel(active, sim_threads, grade);

  // Detected ids in ascending fault-index (slice) order — identical to serial.
  std::vector<int64_t> detected;
  for (const GradeRangeResult& sr : slices) {
    for (const size_t idx : sr.detected_indices) {
      detected.push_back(active[idx].fault_id);
    }
    g_simulation_instrumentation.batch_fault_calls += sr.batch_fault_calls;
  }
  return detected;
}

std::vector<int64_t> simulate_transition_tentative_from_preloaded(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::tuple<int64_t, uint32_t, uint8_t>>& preloaded,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances, int sim_threads) {
  if (preloaded.empty()) {
    return {};
  }
  const CachedGraph& ctx =
      load_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const TestVector v1 = vector_from_map(ctx.parsed, launch, input_order);
  const TestVector v2 = vector_from_map(ctx.parsed, capture, input_order);
  BitParallelSim sim;  // stateless; shared by every slice/thread

  std::vector<ActiveFaultRecord> active;
  active.reserve(preloaded.size());
  for (const auto& [fault_id, net_index, type] : preloaded) {
    CompactFault cf;
    cf.net_index = net_index;
    cf.type = static_cast<FaultType>(type);
    active.push_back({fault_id, cf});
  }

  const auto grade = [&](const FaultBatch& batch) -> uint64_t {
    return sim.simulate_transition_batch(ctx.cg, v1, v2, batch);
  };
  const std::vector<GradeRangeResult> slices =
      grade_active_parallel(active, sim_threads, grade);

  std::vector<int64_t> detected;
  for (const GradeRangeResult& sr : slices) {
    for (const size_t idx : sr.detected_indices) {
      detected.push_back(active[idx].fault_id);
    }
    g_simulation_instrumentation.batch_fault_calls += sr.batch_fault_calls;
  }
  return detected;
}

}  // namespace faultflow::atpg
