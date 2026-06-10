#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

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
}
