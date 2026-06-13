#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "atpg/progressive_atpg.hpp"
#include "atpg/sat_atpg.hpp"
#include "db/sqlite_store.hpp"
#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
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
  d["coverage_percent"] = s.coverage_percent;
  return d;
}

}  // namespace

py::dict simulate_to_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path,
    const std::vector<std::map<std::string, bool>>& raw_vectors,
    const std::vector<std::string>& input_order, const std::string& vector_source,
    bool include_clock_faults, bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy) {
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  EnumeratorOptions options;
  options.include_clock_faults = include_clock_faults;
  options.include_reset_faults = include_reset_faults;
  std::vector<CompactFault> faults = enumerate_faults(ng, cg, options);
  if (collapsing) {
    faults = collapse_primitive_faults(ng, cg, std::move(faults));
  }

  const std::vector<TestVector> vectors = convert_vectors(parsed, raw_vectors);
  BitParallelSim sim;
  for (auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE ||
        fault.collapsed_into != UINT32_MAX) {
      fault.status = FaultStatus::UNDETECTED;
      continue;
    }
    fault.status = FaultStatus::UNDETECTED;
    for (size_t vi = 0; vi < vectors.size(); ++vi) {
      if (sim.simulate_single_fault(cg, vectors[vi], fault)) {
        fault.status = FaultStatus::DETECTED;
        fault.detected_by_vector = static_cast<uint32_t>(vi + 1);
        break;
      }
    }
  }

  db::init_database(db_path);
  const int64_t run_id =
      db::start_run(db_path, vector_source, static_cast<int64_t>(vectors.size()));
  db::write_vectors(db_path, run_id, vector_source,
                    vector_patterns(raw_vectors, input_order));
  db::write_faults(db_path, run_id, ng, cg, faults);
  const db::CoverageSummary summary = db::summarize(db_path);
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
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
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
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
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
    const std::string& db_path, bool include_clock_faults,
    bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy) {
  atpg::ensure_faults_enumerated(json_path, cell_map_path, db_path,
                                 include_clock_faults, include_reset_faults,
                                 collapsing, unsupported_policy);
}

py::dict solve_fault_atpg(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy) {
  const atpg::SolveFaultResult result = atpg::solve_fault_for_db(
      json_path, cell_map_path, db_path, fault_id, blocked_patterns,
      conflict_limit, sat_timeout_seconds, unsupported_policy);
  py::dict out;
  out["result"] = result.result;
  out["vector"] = result.vector;
  return out;
}

bool verify_fault_candidate(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy) {
  return atpg::verify_fault_vector(json_path, cell_map_path, db_path, fault_id,
                                   vector, unsupported_policy);
}

py::list simulate_incremental_py(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy) {
  const std::vector<atpg::ProgressiveDetection> detections =
      atpg::simulate_incremental(json_path, cell_map_path, db_path, run_id,
                                 new_vectors, input_order, fault_ids,
                                 vector_start_index, unsupported_policy);
  py::list out;
  for (const auto& det : detections) {
    py::dict row;
    row["fault_id"] = det.fault_id;
    row["vector_index"] = det.vector_index;
    out.append(row);
  }
  return out;
}

void invalidate_stale_redundant_py(const std::string& db_path,
                                   const std::string& redundancy_model_id) {
  db::invalidate_stale_redundant(db_path, redundancy_model_id);
}

void append_vectors_py(const std::string& db_path, int64_t run_id,
                       const std::string& source,
                       const std::vector<std::string>& patterns,
                       int64_t start_index) {
  db::append_vectors(db_path, run_id, source, patterns, start_index);
}

void mark_fault_redundant_py(const std::string& db_path, int64_t fault_id,
                             const std::string& redundancy_model_id) {
  db::mark_fault_redundant(db_path, fault_id, redundancy_model_id);
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
    const std::string& clock_port, const std::string& scan_enable_port,
    const std::vector<std::string>& scan_input_ports,
    const std::vector<std::string>& scan_output_ports,
    const std::vector<std::string>& functional_output_ports,
    int max_chain_length,
    const std::map<int, std::vector<bool>>& load_seqs,
    const std::map<std::string, bool>& capture_pi_values,
    const std::string& unsupported_policy) {
  scan::ScanPatternRequest request;
  request.clock_port = clock_port;
  request.scan_enable_port = scan_enable_port;
  request.scan_input_ports = scan_input_ports;
  request.scan_output_ports = scan_output_ports;
  request.functional_output_ports = functional_output_ports;
  request.max_chain_length = max_chain_length;
  request.load_seqs = load_seqs;
  request.capture_pi_values = capture_pi_values;
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

}  // namespace faultflow

PYBIND11_MODULE(_faultflow_core, m) {
  m.doc() = "faultflow C++ data-plane bindings";
  m.def("simulate_to_db", &faultflow::simulate_to_db, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("vectors"),
        py::arg("input_order"), py::arg("vector_source"),
        py::arg("include_clock_faults") = false,
        py::arg("include_reset_faults") = false, py::arg("collapsing") = false,
        py::arg("unsupported_policy") = "fail");
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
        py::arg("include_clock_faults") = false,
        py::arg("include_reset_faults") = false, py::arg("collapsing") = false,
        py::arg("unsupported_policy") = "fail");
  m.def("solve_fault_atpg", &faultflow::solve_fault_atpg, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("fault_id"),
        py::arg("blocked_patterns"), py::arg("conflict_limit") = 100000,
        py::arg("sat_timeout_seconds") = 10,
        py::arg("unsupported_policy") = "fail");
  m.def("verify_fault_candidate", &faultflow::verify_fault_candidate,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("fault_id"), py::arg("vector"),
        py::arg("unsupported_policy") = "fail");
  m.def("simulate_incremental", &faultflow::simulate_incremental_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("db_path"),
        py::arg("run_id"), py::arg("new_vectors"), py::arg("input_order"),
        py::arg("fault_ids"), py::arg("vector_start_index"),
        py::arg("unsupported_policy") = "fail");
  m.def("invalidate_stale_redundant", &faultflow::invalidate_stale_redundant_py,
        py::arg("db_path"), py::arg("redundancy_model_id"));
  m.def("append_vectors", &faultflow::append_vectors_py, py::arg("db_path"),
        py::arg("run_id"), py::arg("source"), py::arg("patterns"),
        py::arg("start_index"));
  m.def("mark_fault_redundant", &faultflow::mark_fault_redundant_py,
        py::arg("db_path"), py::arg("fault_id"), py::arg("redundancy_model_id"));
  m.def("complete_run_with_atpg", &faultflow::complete_run_with_atpg_py,
        py::arg("db_path"), py::arg("run_id"), py::arg("coverage_percent"),
        py::arg("terminal_reason"), py::arg("rounds"), py::arg("sat"),
        py::arg("unsat"), py::arg("timeout"), py::arg("unknown"),
        py::arg("rejected"), py::arg("generated"), py::arg("accepted"));
  m.def("update_run_vector_count", &faultflow::update_run_vector_count_py,
        py::arg("db_path"), py::arg("run_id"), py::arg("vector_count"));
  m.def("simulate_scan_pattern", &faultflow::simulate_scan_pattern_py,
        py::arg("json_path"), py::arg("cell_map_path"), py::arg("clock_port"),
        py::arg("scan_enable_port"), py::arg("scan_input_ports"),
        py::arg("scan_output_ports"), py::arg("functional_output_ports"),
        py::arg("max_chain_length"), py::arg("load_seqs"),
        py::arg("capture_pi_values"), py::arg("unsupported_policy") = "fail");
}
