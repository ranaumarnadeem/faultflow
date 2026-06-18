#pragma once

#include <string>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::test {

struct VectorSet {
  std::vector<TestVector> vectors;
};

std::string fixture_path(const std::string& name);

std::string benchmark_path(const std::string& name);

ParsedGraph load_parsed(const std::string& fixture_name);

ParsedGraph load_parsed_benchmark(const std::string& relative_path);

NormalizedGraph load_normalized(const std::string& fixture_name);

NormalizedGraph load_normalized_benchmark(const std::string& relative_path);

CompiledSimGraph load_compiled(const std::string& fixture_name);

CompiledSimGraph load_compiled_benchmark(const std::string& relative_path);

std::string cell_map_path();
std::string cell_map_path_osu();

VectorSet generate_complete_input_space(const std::vector<int>& pi_yosys_ids);

int net_id(const ParsedGraph& pg, const std::string& name);

struct ParallelMismatch {
  uint32_t fault_net = 0;
  FaultType fault_type = FaultType::SA0;
  size_t vector_index = 0;
  bool golden_detected = false;
  bool parallel_detected = false;
};

std::vector<ParallelMismatch> verify_parallel_matches_golden(
    const CompiledSimGraph& cg, const NormalizedGraph& ng,
    const std::vector<CompactFault>& faults, const VectorSet& vectors);

double run_coverage_exhaustive(const CompiledSimGraph& cg,
                               const NormalizedGraph& ng,
                               const VectorSet& vectors);

}  // namespace faultflow::test
