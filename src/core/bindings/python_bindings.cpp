#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "atpg/compaction.hpp"
#include "atpg/progressive_atpg.hpp"
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
#include "scan/scan_pattern_sim.hpp"

namespace py = pybind11;

namespace faultflow {
namespace {

std::vector<TestVector> convert_vectors(
    const ParsedGraph& parsed,
    const std::vector<std::map<std::string, bool>>& raw_vectors) {
  std::vector<TestVector> out;
  out.reserve(raw_vectors.size());
  for (const auto& raw : raw_vectors) {
    TestVector tv;
    for (const auto& [name, value] : raw) {
      tv.inputs[parsed.net_id_by_name(name)] = value;
    }
    out.push_back(std::move(tv));
  }
  return out;
}

std::vector<TestVector> convert_vectors_strict(
    const ParsedGraph& parsed,
    const std::vector<std::map<std::string, bool>>& raw_vectors,
    const std::vector<std::string>& input_order) {
  std::vector<TestVector> out;
  out.reserve(raw_vectors.size());
  for (size_t vi = 0; vi < raw_vectors.size(); ++vi) {
    const auto& raw = raw_vectors[vi];
    TestVector tv;
    for (const auto& input : input_order) {
      const auto it = raw.find(input);
      if (it == raw.end()) {
        throw std::runtime_error("vector " + std::to_string(vi + 1) +
                                 ": missing PI " + input);
      }
      tv.inputs[parsed.net_id_by_name(input)] = it->second;
    }
    out.push_back(std::move(tv));
  }
  return out;
}

std::vector<TestVector> convert_sequence_vectors_strict(
    const ParsedGraph& parsed,
    const std::vector<std::vector<std::map<std::string, bool>>>& raw_sequences,
    const std::vector<std::string>& input_order) {
  std::vector<TestVector> out;
  out.reserve(raw_sequences.size());
  for (size_t vi = 0; vi < raw_sequences.size(); ++vi) {
    if (raw_sequences[vi].empty()) {
      throw std::runtime_error("sequence " + std::to_string(vi + 1) +
                               ": empty cycle sequence");
    }
    TestVector tv;
    for (size_t ci = 0; ci < raw_sequences[vi].size(); ++ci) {
      const auto& raw = raw_sequences[vi][ci];
      TestCycle cycle;
      cycle.sample_outputs = ci + 1 == raw_sequences[vi].size();
      for (const auto& input : input_order) {
        const auto it = raw.find(input);
        if (it == raw.end()) {
          throw std::runtime_error("sequence " + std::to_string(vi + 1) +
                                   " cycle " + std::to_string(ci + 1) +
                                   ": missing PI " + input);
        }
        cycle.inputs[parsed.net_id_by_name(input)] = it->second;
      }
      tv.cycles.push_back(std::move(cycle));
    }
    out.push_back(std::move(tv));
  }
  return out;
}

std::vector<std::string> vector_patterns(
    const std::vector<std::map<std::string, bool>>& raw_vectors,
    const std::vector<std::string>& input_order) {
  std::vector<std::string> patterns;
  patterns.reserve(raw_vectors.size());
  for (const auto& raw : raw_vectors) {
    std::string p;
    p.reserve(input_order.size());
    for (const auto& input : input_order) {
      const auto it = raw.find(input);
      p += (it != raw.end() && it->second) ? '1' : '0';
    }
    patterns.push_back(std::move(p));
  }
  return patterns;
}

py::dict summary_to_dict(const db::CoverageSummary& s) {
  py::dict d;
  d["total_raw_faults"] = s.total_raw_faults;
  d["denominator"] = s.denominator;
  d["detected"] = s.detected;
  d["undetected"] = s.undetected;
  d["redundant"] = s.redundant;
  d["collapsed"] = s.collapsed;
  d["excluded_blackbox"] = s.excluded_blackbox;
  d["excluded_clock"] = s.excluded_clock;
  d["excluded_reset"] = s.excluded_reset;
  d["excluded_scan"] = s.excluded_scan;
  d["excluded_scan_internal"] = s.excluded_scan_internal;
  d["excluded_scan_chain"] = s.excluded_scan_chain;
  d["coverage_percent"] = s.coverage_percent;
  return d;
}

}  // namespace

py::dict simulate_to_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id,
    const std::vector<std::map<std::string, bool>>& raw_vectors,
    const std::vector<std::string>& input_order, const std::string& vector_source,
    bool include_clock_faults, bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode = "") {
  const CachedGraph& graph = load_cached_graph(
      json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const ParsedGraph& parsed = graph.parsed;
  const NormalizedGraph& ng = graph.ng;
  const CompiledSimGraph& cg = graph.cg;

  EnumeratorOptions options;
  options.include_clock_faults = include_clock_faults;
  options.include_reset_faults = include_reset_faults;
  std::vector<CompactFault> faults = enumerate_faults(ng, cg, options);
  if (collapsing) {
    faults = collapse_primitive_faults(ng, cg, std::move(faults));
  }

  const TestMode mode = parse_test_mode(test_mode);
  const bool use_mode = (mode != TestMode::FUNCTIONAL) && !cg.wrapper_cells.empty();
  const ModeConfig mc = use_mode ? build_mode_config(cg, mode) : ModeConfig{};

  const std::vector<TestVector> vectors = convert_vectors(parsed, raw_vectors);
  BitParallelSim sim;
  std::vector<size_t> active;
  active.reserve(faults.size());
  for (size_t i = 0; i < faults.size(); ++i) {
    CompactFault& fault = faults[i];
    fault.status = FaultStatus::UNDETECTED;
    if (fault.exclusion == FaultExclusion::NONE &&
        fault.collapsed_into == UINT32_MAX) {
      active.push_back(i);
    }
  }
  for (size_t vi = 0; vi < vectors.size() && !active.empty(); ++vi) {
    std::vector<size_t> still_active;
    still_active.reserve(active.size());
    for (size_t begin = 0; begin < active.size(); begin += kBatchSize) {
      const size_t end = std::min(begin + kBatchSize, active.size());
      FaultBatch batch;
      batch.size = static_cast<int>(end - begin);
      for (size_t i = begin; i < end; ++i) {
        CompactFault lane = faults[active[i]];
        lane.bit = static_cast<uint8_t>((i - begin) + 1);
        lane.sa_mask = 1ULL << lane.bit;
        batch.faults[i - begin] = lane;
        batch.mask |= lane.sa_mask;
      }
      const uint64_t detected_mask = use_mode
          ? sim.simulate_batch(cg, vectors[vi], batch, mc)
          : sim.simulate_batch(cg, vectors[vi], batch);
      for (size_t i = begin; i < end; ++i) {
        const CompactFault& lane = batch.faults[i - begin];
        CompactFault& fault = faults[active[i]];
        if ((detected_mask & lane.sa_mask) != 0) {
          fault.status = FaultStatus::DETECTED;
          fault.detected_by_vector = static_cast<uint32_t>(vi + 1);
        } else {
          still_active.push_back(active[i]);
        }
      }
    }
    active = std::move(still_active);
  }

  db::init_database(db_path);
  const int64_t run_id = db::start_run(
      db_path, campaign_id, vector_source, static_cast<int64_t>(vectors.size()));
  db::write_vectors(db_path, campaign_id, run_id, vector_source,
                    vector_patterns(raw_vectors, input_order));
  db::write_faults(db_path, campaign_id, run_id, ng, cg, faults);
  const db::CoverageSummary summary = db::summarize(db_path, campaign_id);
  db::complete_run(db_path, run_id, summary.coverage_percent);
  py::dict result = summary_to_dict(summary);
  result["run_id"] = run_id;
  return result;
}

std::vector<std::map<std::string, bool>> fault_free_outputs(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::map<std::string, bool>>& raw_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<std::string>& output_order,
    const std::string& unsupported_policy) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;
  const std::vector<TestVector> vectors =
      convert_vectors_strict(parsed, raw_vectors, input_order);

  GoldenRefSim ref;
  std::vector<std::map<std::string, bool>> out;
  out.reserve(vectors.size());
  for (const TestVector& vector : vectors) {
    const std::map<int, bool> values = ref.simulate_fault_free(cg, vector);
    std::map<std::string, bool> sample;
    for (const auto& output : output_order) {
      const int yid = parsed.net_id_by_name(output);
      const auto it = values.find(yid);
      if (it == values.end()) {
        throw std::runtime_error("output not simulated: " + output);
      }
      sample[output] = it->second;
    }
    out.push_back(std::move(sample));
  }
  return out;
}

std::vector<std::map<std::string, bool>> fault_free_sequence_outputs(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::vector<std::map<std::string, bool>>>& raw_sequences,
    const std::vector<std::string>& input_order,
    const std::vector<std::string>& output_order,
    const std::string& unsupported_policy) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;
  const std::vector<TestVector> vectors =
      convert_sequence_vectors_strict(parsed, raw_sequences, input_order);

  GoldenRefSim ref;
  std::vector<std::map<std::string, bool>> out;
  out.reserve(vectors.size());
  for (const TestVector& vector : vectors) {
    const auto samples = ref.simulate_sequence_fault_free(cg, vector);
    if (samples.empty()) {
      throw std::runtime_error("sequence produced no sampled outputs");
    }
    const std::map<int, bool>& values = samples.back();
    std::map<std::string, bool> sample;
    for (const auto& output : output_order) {
      const int yid = parsed.net_id_by_name(output);
      const auto it = values.find(yid);
      if (it == values.end()) {
        throw std::runtime_error("output not simulated: " + output);
      }
      sample[output] = it->second;
    }
    out.push_back(std::move(sample));
  }
  return out;
}

std::vector<std::map<std::string, bool>> atpg_random_vectors(
    const std::vector<std::string>& input_order, int count, uint64_t seed) {
  return atpg::generate_random_vectors(input_order, count, seed);
}

void ensure_faults_enumerated_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, bool include_clock_faults,
    bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  atpg::ensure_faults_enumerated(json_path, cell_map_path, db_path, campaign_id,
                                 include_clock_faults, include_reset_faults,
                                 collapsing, unsupported_policy,
                                 blackbox_instances);
}

py::dict solve_fault_atpg(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode = "", bool cone_restrict = true) {
  const atpg::SolveFaultResult result = atpg::solve_fault_for_db(
      json_path, cell_map_path, db_path, fault_id, blocked_patterns,
      conflict_limit, sat_timeout_seconds, unsupported_policy,
      blackbox_instances, test_mode, cone_restrict);
  py::dict out;
  out["result"] = result.result;
  out["vector"] = result.vector;
  return out;
}

bool verify_fault_candidate(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode = "") {
  return atpg::verify_fault_vector(json_path, cell_map_path, db_path, fault_id,
                                   vector, unsupported_policy,
                                   blackbox_instances, test_mode);
}

py::list simulate_incremental_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode = "") {
  const std::vector<atpg::ProgressiveDetection> detections =
      atpg::simulate_incremental(json_path, cell_map_path, db_path, campaign_id,
                                 run_id, new_vectors, input_order, fault_ids,
                                 vector_start_index, unsupported_policy,
                                 blackbox_instances, test_mode);
  py::list out;
  for (const auto& det : detections) {
    py::dict row;
    row["fault_id"] = det.fault_id;
    row["vector_index"] = det.vector_index;
    out.append(row);
  }
  return out;
}

py::list simulate_tentative_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path,
    const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    const std::string& test_mode = "") {
  const std::vector<int64_t> detected = atpg::simulate_tentative_detections(
      json_path, cell_map_path, db_path, vector, input_order, fault_ids,
      unsupported_policy, blackbox_instances, test_mode);
  py::list out;
  for (int64_t fault_id : detected) {
    out.append(fault_id);
  }
  return out;
}

// ---- Transition model bindings (combinational broadside two-pattern) -------

std::vector<atpg::VectorPair> atpg_random_vector_pairs(
    const std::vector<std::string>& input_order, int count, uint64_t seed) {
  return atpg::generate_random_vector_pairs(input_order, count, seed);
}

py::dict solve_transition_fault_atpg(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    bool cone_restrict = true) {
  const atpg::SolveTransitionResult result =
      atpg::solve_transition_fault_for_db(
          json_path, cell_map_path, db_path, fault_id, blocked_patterns,
          conflict_limit, sat_timeout_seconds, unsupported_policy,
          blackbox_instances, cone_restrict);
  py::dict out;
  out["result"] = result.result;
  out["launch"] = result.launch;
  out["capture"] = result.capture;
  return out;
}

py::dict solve_scan_transition_fault_atpg(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    bool cone_restrict = true) {
  const atpg::SolveTransitionResult result =
      atpg::solve_scan_transition_fault_for_db(
          json_path, cell_map_path, db_path, fault_id, blocked_patterns,
          conflict_limit, sat_timeout_seconds, unsupported_policy,
          blackbox_instances, cone_restrict);
  py::dict out;
  out["result"] = result.result;
  out["launch"] = result.launch;
  out["capture"] = result.capture;
  return out;
}

py::dict solve_scan_los_transition_fault_atpg(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::pair<std::string, std::string>>& couple_ports,
    const std::vector<std::string>& head_ppi_ports,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances,
    bool cone_restrict = true) {
  const atpg::SolveTransitionResult result =
      atpg::solve_scan_los_transition_fault_for_db(
          json_path, cell_map_path, db_path, fault_id, couple_ports,
          head_ppi_ports, blocked_patterns, conflict_limit, sat_timeout_seconds,
          unsupported_policy, blackbox_instances, cone_restrict);
  py::dict out;
  out["result"] = result.result;
  out["launch"] = result.launch;
  out["capture"] = result.capture;
  return out;
}

bool verify_transition_candidate(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  return atpg::verify_transition_fault_vector(json_path, cell_map_path, db_path,
                                              fault_id, launch, capture,
                                              unsupported_policy,
                                              blackbox_instances);
}

py::list simulate_transition_incremental_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<atpg::VectorPair>& new_pairs,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  const std::vector<atpg::ProgressiveDetection> detections =
      atpg::simulate_transition_incremental(
          json_path, cell_map_path, db_path, campaign_id, run_id, new_pairs,
          input_order, fault_ids, vector_start_index, unsupported_policy,
          blackbox_instances);
  py::list out;
  for (const auto& det : detections) {
    py::dict row;
    row["fault_id"] = det.fault_id;
    row["vector_index"] = det.vector_index;
    out.append(row);
  }
  return out;
}

py::list simulate_transition_tentative_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  const std::vector<int64_t> detected =
      atpg::simulate_transition_tentative_detections(
          json_path, cell_map_path, db_path, launch, capture, input_order,
          fault_ids, unsupported_policy, blackbox_instances);
  py::list out;
  for (int64_t fault_id : detected) {
    out.append(fault_id);
  }
  return out;
}

py::list compaction_detections_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path,
    const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  const std::vector<int64_t> detected = atpg::detect_with_vector_unfiltered(
      json_path, cell_map_path, db_path, vector, input_order, fault_ids,
      unsupported_policy, blackbox_instances);
  py::list out;
  for (int64_t fault_id : detected) {
    out.append(fault_id);
  }
  return out;
}

py::list compaction_pair_detections_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  const std::vector<int64_t> detected = atpg::detect_with_pair_unfiltered(
      json_path, cell_map_path, db_path, launch, capture, input_order, fault_ids,
      unsupported_policy, blackbox_instances);
  py::list out;
  for (int64_t fault_id : detected) {
    out.append(fault_id);
  }
  return out;
}

void invalidate_stale_redundant_py(const std::string& db_path,
                                   int64_t campaign_id,
                                   const std::string& redundancy_model_id) {
  db::invalidate_stale_redundant(db_path, campaign_id, redundancy_model_id);
}

void append_vectors_py(const std::string& db_path, int64_t campaign_id,
                       int64_t run_id, const std::string& source,
                       const std::vector<std::string>& patterns,
                       int64_t start_index,
                       const std::vector<std::string>& launch_patterns) {
  db::append_vectors(db_path, campaign_id, run_id, source, patterns,
                     start_index, launch_patterns);
}

void mark_fault_redundant_py(const std::string& db_path, int64_t fault_id,
                             const std::string& redundancy_model_id) {
  db::mark_fault_redundant(db_path, fault_id, redundancy_model_id);
}

void mark_fault_protocol_unresolved_py(const std::string& db_path,
                                       int64_t fault_id) {
  db::mark_fault_protocol_unresolved(db_path, fault_id);
}

void complete_run_with_atpg_py(const std::string& db_path, int64_t run_id,
                               double coverage_percent,
                               const std::string& terminal_reason, int rounds,
                               int sat, int unsat, int timeout, int unknown,
                               int rejected, int generated, int accepted) {
  db::AtpgRunStats stats;
  stats.terminal_reason = terminal_reason;
  stats.rounds = rounds;
  stats.sat = sat;
  stats.unsat = unsat;
  stats.timeout = timeout;
  stats.unknown = unknown;
  stats.rejected_candidates = rejected;
  stats.generated_vectors = generated;
  stats.accepted_vectors = accepted;
  db::complete_run_with_atpg(db_path, run_id, coverage_percent, stats);
}

void update_run_vector_count_py(const std::string& db_path, int64_t run_id,
                              int64_t vector_count) {
  db::update_run_vector_count(db_path, run_id, vector_count);
}

py::dict simulate_scan_pattern_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::string>& clock_ports,
    const std::vector<bool>& clock_off_states,
    const std::string& scan_enable_port,
    const std::vector<std::string>& scan_input_ports,
    const std::vector<std::string>& scan_output_ports,
    const std::vector<std::string>& functional_output_ports,
    int max_chain_length,
    const std::map<int, std::vector<bool>>& load_seqs,
    const std::map<std::string, bool>& capture_pi_values,
    const std::string& unsupported_policy, bool loc_two_capture,
    bool los_two_capture,
    const std::map<int, bool>& los_launch_scan_in,
    const std::vector<std::string>& active_clock_ports,
    const std::string& test_mode = "") {
  scan::ScanPatternRequest request;
  request.clock_ports = clock_ports;
  request.clock_off_states = clock_off_states;
  request.scan_enable_port = scan_enable_port;
  request.scan_input_ports = scan_input_ports;
  request.scan_output_ports = scan_output_ports;
  request.functional_output_ports = functional_output_ports;
  request.max_chain_length = max_chain_length;
  request.load_seqs = load_seqs;
  request.capture_pi_values = capture_pi_values;
  request.loc_two_capture = loc_two_capture;
  request.los_two_capture = los_two_capture;
  request.los_launch_scan_in = los_launch_scan_in;
  request.active_clock_ports = active_clock_ports;
  request.test_mode = parse_test_mode(test_mode);
  const scan::ScanPatternResult result = scan::simulate_scan_pattern(
      json_path, cell_map_path, request, unsupported_policy);
  py::dict out;
  out["real_po_values"] = result.real_po_values;
  py::dict unload;
  for (const auto& [chain_id, bits] : result.unload_seqs) {
    unload[py::int_(chain_id)] = bits;
  }
  out["unload_seqs"] = unload;
  return out;
}

py::dict simulate_scan_protocol_faults_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::string>& clock_ports,
    const std::vector<bool>& clock_off_states,
    const std::string& scan_enable_port,
    const std::vector<std::string>& scan_input_ports,
    const std::vector<std::string>& scan_output_ports,
    const std::vector<std::string>& functional_output_ports,
    int max_chain_length,
    const std::map<int, std::vector<bool>>& load_seqs,
    const std::map<std::string, bool>& capture_pi_values,
    const std::vector<std::pair<uint32_t, uint8_t>>& faults,
    const std::string& unsupported_policy, bool loc_two_capture,
    bool los_two_capture, const std::map<int, bool>& los_launch_scan_in,
    const std::vector<std::string>& active_clock_ports,
    const std::string& test_mode = "") {
  scan::ScanProtocolFaultRequest request;
  request.pattern.clock_ports = clock_ports;
  request.pattern.clock_off_states = clock_off_states;
  request.pattern.scan_enable_port = scan_enable_port;
  request.pattern.scan_input_ports = scan_input_ports;
  request.pattern.scan_output_ports = scan_output_ports;
  request.pattern.functional_output_ports = functional_output_ports;
  request.pattern.max_chain_length = max_chain_length;
  request.pattern.load_seqs = load_seqs;
  request.pattern.capture_pi_values = capture_pi_values;
  request.pattern.loc_two_capture = loc_two_capture;
  request.pattern.los_two_capture = los_two_capture;
  request.pattern.los_launch_scan_in = los_launch_scan_in;
  request.pattern.active_clock_ports = active_clock_ports;
  request.pattern.test_mode = parse_test_mode(test_mode);
  request.faults.reserve(faults.size());
  for (const auto& [net_index, fault_type] : faults) {
    scan::ScanProtocolFaultSpec spec;
    spec.compiled_net_index = net_index;
    spec.fault_type = fault_type;
    request.faults.push_back(spec);
  }
  const scan::ScanProtocolFaultSimResult result =
      scan::simulate_scan_protocol_faults(
      json_path, cell_map_path, request, unsupported_policy);
  py::dict out;
  out["golden_real_po_values"] = result.golden.real_po_values;
  py::dict golden_unload;
  for (const auto& [chain_id, bits] : result.golden.unload_seqs) {
    golden_unload[py::int_(chain_id)] = bits;
  }
  out["golden_unload_seqs"] = golden_unload;
  py::list batches;
  for (const auto& batch : result.batches) {
    py::dict batch_dict;
    batch_dict["batch_index"] = batch.batch_index;
    py::list lanes;
    for (const auto& lane : batch.lanes) {
      py::dict lane_dict;
      lane_dict["fault_index"] = lane.fault_index;
      lane_dict["outcome"] =
          lane.outcome == scan::ScanProtocolFaultOutcome::PASS
              ? "pass"
              : "no_capture_or_unload_effect";
      lanes.append(lane_dict);
    }
    batch_dict["lanes"] = lanes;
    batches.append(batch_dict);
  }
  out["batches"] = batches;
  return out;
}

py::list list_site_keys_py(const std::string& json_path,
                           const std::string& cell_map_path,
                           const std::string& unsupported_policy) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  const CompiledSimGraph& cg = graph.cg;
  py::list out;
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    py::dict row;
    row["compiled_net_index"] = cidx;
    row["yosys_net_id"] = cg.compiled_to_yosys[cidx];
    row["site_key"] = canonical_site_key(cg, cidx);
    if (cidx < cg.net_sites.size() &&
        cg.net_sites[cidx].kind == SiteKind::BRANCH) {
      row["kind"] = "branch";
      row["consumer_instance"] = cg.net_sites[cidx].consumer_instance;
      row["input_pin"] = cg.net_sites[cidx].input_pin;
    } else {
      row["kind"] = "stem";
    }
    out.append(row);
  }
  return out;
}

}  // namespace faultflow

PYBIND11_MODULE(_faultflow_core, m) {
  m.doc() = "faultflow C++ data-plane bindings";
  m.def("simulate_to_db", &faultflow::simulate_to_db, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("campaign_id"),
        py::arg("vectors"), py::arg("input_order"), py::arg("vector_source"),
        py::arg("include_clock_faults") = false,
        py::arg("include_reset_faults") = false, py::arg("collapsing") = false,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  m.def("fault_free_outputs", &faultflow::fault_free_outputs, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("vectors"), py::arg("input_order"),
        py::arg("output_order"), py::arg("unsupported_policy") = "fail");
  m.def("fault_free_sequence_outputs", &faultflow::fault_free_sequence_outputs,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("sequences"),
        py::arg("input_order"), py::arg("output_order"),
        py::arg("unsupported_policy") = "fail");
  m.def("atpg_random_vectors", &faultflow::atpg_random_vectors,
        py::arg("input_order"), py::arg("count"), py::arg("seed"));
  m.def("ensure_faults_enumerated", &faultflow::ensure_faults_enumerated_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("campaign_id"), py::arg("include_clock_faults") = false,
        py::arg("include_reset_faults") = false, py::arg("collapsing") = false,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("solve_fault_atpg", &faultflow::solve_fault_atpg, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("fault_id"),
        py::arg("blocked_patterns"), py::arg("conflict_limit") = 100000,
        py::arg("sat_timeout_seconds") = 10,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("test_mode") = "", py::arg("cone_restrict") = true);
  m.def("verify_fault_candidate", &faultflow::verify_fault_candidate,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("fault_id"), py::arg("vector"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  m.def("simulate_incremental", &faultflow::simulate_incremental_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("campaign_id"), py::arg("run_id"), py::arg("new_vectors"),
        py::arg("input_order"), py::arg("fault_ids"),
        py::arg("vector_start_index"), py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  m.def("simulate_tentative", &faultflow::simulate_tentative_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("vector"), py::arg("input_order"), py::arg("fault_ids"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  // Transition model (combinational broadside two-pattern) entry points.
  m.def("atpg_random_vector_pairs", &faultflow::atpg_random_vector_pairs,
        py::arg("input_order"), py::arg("count"), py::arg("seed"));
  m.def("solve_transition_fault_atpg", &faultflow::solve_transition_fault_atpg,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("fault_id"), py::arg("blocked_patterns"),
        py::arg("conflict_limit") = 100000, py::arg("sat_timeout_seconds") = 10,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("cone_restrict") = true);
  m.def("solve_scan_transition_fault_atpg",
        &faultflow::solve_scan_transition_fault_atpg, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("fault_id"),
        py::arg("blocked_patterns"), py::arg("conflict_limit") = 100000,
        py::arg("sat_timeout_seconds") = 10,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("cone_restrict") = true);
  m.def("solve_scan_los_transition_fault_atpg",
        &faultflow::solve_scan_los_transition_fault_atpg, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("fault_id"),
        py::arg("couple_ports"), py::arg("head_ppi_ports"),
        py::arg("blocked_patterns"), py::arg("conflict_limit") = 100000,
        py::arg("sat_timeout_seconds") = 10,
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{},
        py::arg("cone_restrict") = true);
  m.def("verify_transition_candidate", &faultflow::verify_transition_candidate,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("fault_id"), py::arg("launch"), py::arg("capture"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("simulate_transition_incremental",
        &faultflow::simulate_transition_incremental_py, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("campaign_id"),
        py::arg("run_id"), py::arg("new_pairs"), py::arg("input_order"),
        py::arg("fault_ids"), py::arg("vector_start_index"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("simulate_transition_tentative",
        &faultflow::simulate_transition_tentative_py, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("launch"),
        py::arg("capture"), py::arg("input_order"), py::arg("fault_ids"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("compaction_detections", &faultflow::compaction_detections_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("vector"), py::arg("input_order"), py::arg("fault_ids"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("compaction_pair_detections",
        &faultflow::compaction_pair_detections_py, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("launch"),
        py::arg("capture"), py::arg("input_order"), py::arg("fault_ids"),
        py::arg("unsupported_policy") = "fail",
        py::arg("blackbox_instances") = std::vector<std::string>{});
  m.def("invalidate_stale_redundant", &faultflow::invalidate_stale_redundant_py,
        py::arg("db_path"), py::arg("campaign_id"),
        py::arg("redundancy_model_id"));
  m.def("append_vectors", &faultflow::append_vectors_py, py::arg("db_path"),
        py::arg("campaign_id"), py::arg("run_id"), py::arg("source"),
        py::arg("patterns"), py::arg("start_index"),
        py::arg("launch_patterns") = std::vector<std::string>{});
  m.def("mark_fault_redundant", &faultflow::mark_fault_redundant_py,
        py::arg("db_path"), py::arg("fault_id"), py::arg("redundancy_model_id"));
  m.def("mark_fault_protocol_unresolved",
        &faultflow::mark_fault_protocol_unresolved_py, py::arg("db_path"),
        py::arg("fault_id"));
  m.def("complete_run_with_atpg", &faultflow::complete_run_with_atpg_py,
        py::arg("db_path"), py::arg("run_id"), py::arg("coverage_percent"),
        py::arg("terminal_reason"), py::arg("rounds"), py::arg("sat"),
        py::arg("unsat"), py::arg("timeout"), py::arg("unknown"),
        py::arg("rejected"), py::arg("generated"), py::arg("accepted"));
  m.def("update_run_vector_count", &faultflow::update_run_vector_count_py,
        py::arg("db_path"), py::arg("run_id"), py::arg("vector_count"));
  m.def("simulate_scan_pattern", &faultflow::simulate_scan_pattern_py,
        py::arg("json_path"), py::arg("cell_map_path"),
        py::arg("clock_ports"),
        py::arg("clock_off_states") = std::vector<bool>{},
        py::arg("scan_enable_port"), py::arg("scan_input_ports"),
        py::arg("scan_output_ports"), py::arg("functional_output_ports"),
        py::arg("max_chain_length"), py::arg("load_seqs"),
        py::arg("capture_pi_values"), py::arg("unsupported_policy") = "fail",
        py::arg("loc_two_capture") = false, py::arg("los_two_capture") = false,
        py::arg("los_launch_scan_in") = std::map<int, bool>{},
        py::arg("active_clock_ports") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  m.def("simulate_scan_protocol_faults",
        &faultflow::simulate_scan_protocol_faults_py,
        py::arg("json_path"), py::arg("cell_map_path"),
        py::arg("clock_ports"),
        py::arg("clock_off_states") = std::vector<bool>{},
        py::arg("scan_enable_port"), py::arg("scan_input_ports"),
        py::arg("scan_output_ports"), py::arg("functional_output_ports"),
        py::arg("max_chain_length"), py::arg("load_seqs"),
        py::arg("capture_pi_values"), py::arg("faults"),
        py::arg("unsupported_policy") = "fail",
        py::arg("loc_two_capture") = false, py::arg("los_two_capture") = false,
        py::arg("los_launch_scan_in") = std::map<int, bool>{},
        py::arg("active_clock_ports") = std::vector<std::string>{},
        py::arg("test_mode") = "");
  m.def("list_site_keys", &faultflow::list_site_keys_py, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("unsupported_policy") = "fail");
}
