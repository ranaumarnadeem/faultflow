#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <map>
#include <string>
#include <vector>

#include "db/phase1_db.hpp"
#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
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

}  // namespace faultflow

PYBIND11_MODULE(_faultflow_core, m) {
  m.doc() = "faultflow Phase 1 C++ data-plane bindings";
  m.def("simulate_to_db", &faultflow::simulate_to_db, py::arg("json_path"),
        py::arg("cell_map_path"), py::arg("db_path"), py::arg("vectors"),
        py::arg("input_order"), py::arg("vector_source"),
        py::arg("include_clock_faults") = false,
        py::arg("include_reset_faults") = false, py::arg("collapsing") = false,
        py::arg("unsupported_policy") = "fail");
}
