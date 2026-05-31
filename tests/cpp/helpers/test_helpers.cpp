#include "helpers/test_helpers.hpp"

#include "fault/enumerator/fault_enumerator.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"

namespace faultflow::test {

std::string fixture_path(const std::string& name) {
  return std::string(FAULTFLOW_FIXTURE_DIR) + "/" + name;
}

std::string benchmark_path(const std::string& name) {
  return std::string(FAULTFLOW_SOURCE_DIR) + "/tests/benchmarks/" + name;
}

std::string cell_map_path() {
  return std::string(FAULTFLOW_SOURCE_DIR) + "/cells/osu/osu035.json";
}

ParsedGraph load_parsed(const std::string& fixture_name) {
  return ParsedGraph::from_file(fixture_path(fixture_name));
}

ParsedGraph load_parsed_benchmark(const std::string& relative_path) {
  return ParsedGraph::from_file(benchmark_path(relative_path));
}

NormalizedGraph load_normalized(const std::string& fixture_name) {
  const ParsedGraph pg = load_parsed(fixture_name);
  const CellMap map = CellMap::load(cell_map_path());
  return NormalizedGraph::from_parsed(pg, map);
}

NormalizedGraph load_normalized_benchmark(const std::string& relative_path) {
  const ParsedGraph pg = load_parsed_benchmark(relative_path);
  const CellMap map = CellMap::load(cell_map_path());
  return NormalizedGraph::from_parsed(pg, map);
}

CompiledSimGraph load_compiled(const std::string& fixture_name) {
  const NormalizedGraph ng = load_normalized(fixture_name);
  return GraphCompiler::compile(ng);
}

CompiledSimGraph load_compiled_benchmark(const std::string& relative_path) {
  const NormalizedGraph ng = load_normalized_benchmark(relative_path);
  return GraphCompiler::compile(ng);
}

VectorSet generate_complete_input_space(const std::vector<int>& pi_yosys_ids) {
  VectorSet vs;
  const size_t n = pi_yosys_ids.size();
  const size_t count = (n == 0) ? 1 : (1ULL << n);
  for (size_t mask = 0; mask < count; ++mask) {
    TestVector tv;
    for (size_t i = 0; i < n; ++i) {
      tv.inputs[pi_yosys_ids[i]] = ((mask >> i) & 1) != 0;
    }
    vs.vectors.push_back(std::move(tv));
  }
  return vs;
}

int net_id(const ParsedGraph& pg, const std::string& name) {
  return pg.net_id_by_name(name);
}

std::vector<ParallelMismatch> verify_parallel_matches_golden(
    const CompiledSimGraph& cg, const NormalizedGraph& ng,
    const std::vector<CompactFault>& faults, const VectorSet& vectors) {
  GoldenRefSim golden;
  BitParallelSim parallel;
  std::vector<ParallelMismatch> mismatches;

  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    for (size_t vi = 0; vi < vectors.vectors.size(); ++vi) {
      const TestVector& vec = vectors.vectors[vi];
      const auto ff = golden.simulate_fault_free(cg, vec);
      const auto fa = golden.simulate_with_fault(cg, vec, fault);
      const bool g_detect = golden.is_detected(cg, ff, fa);
      const bool p_detect = parallel.simulate_single_fault(cg, vec, fault);
      if (g_detect != p_detect) {
        mismatches.push_back(
            {fault.net_index, fault.type, vi, g_detect, p_detect});
      }
    }
  }
  (void)ng;
  return mismatches;
}

double run_coverage_exhaustive(const CompiledSimGraph& cg,
                               const NormalizedGraph& ng,
                               const VectorSet& vectors) {
  GoldenRefSim golden;
  const auto faults = enumerate_faults(ng, cg);
  size_t active = 0;
  size_t detected = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    ++active;
    bool found = false;
    for (const auto& vec : vectors.vectors) {
      const auto ff = golden.simulate_fault_free(cg, vec);
      const auto fa = golden.simulate_with_fault(cg, vec, fault);
      if (golden.is_detected(cg, ff, fa)) {
        found = true;
        break;
      }
    }
    if (found) {
      ++detected;
    }
  }
  if (active == 0) {
    return 100.0;
  }
  return 100.0 * static_cast<double>(detected) / static_cast<double>(active);
}

}  // namespace faultflow::test
