#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace faultflow::atpg {

struct ProgressiveDetection {
  int64_t fault_id = 0;
  int64_t vector_index = 0;
};

struct SolveFaultResult {
  std::string result;
  std::map<std::string, bool> vector;
};

// A launch/capture pattern pair for combinational (broadside) transition ATPG.
using VectorPair =
    std::pair<std::map<std::string, bool>, std::map<std::string, bool>>;

struct SolveTransitionResult {
  std::string result;  // SAT | UNSAT | TIMEOUT | UNKNOWN
  std::map<std::string, bool> launch;
  std::map<std::string, bool> capture;
};

struct SimulationInstrumentation {
  int64_t single_fault_calls = 0;
  int64_t batch_fault_calls = 0;
  int64_t load_graph_calls = 0;
};

void reset_simulation_instrumentation();
SimulationInstrumentation simulation_instrumentation();

std::vector<std::map<std::string, bool>> generate_random_vectors(
    const std::vector<std::string>& input_order, int count, uint64_t seed);

// Random launch/capture pairs for combinational transition fault simulation.
std::vector<VectorPair> generate_random_vector_pairs(
    const std::vector<std::string>& input_order, int count, uint64_t seed);

void ensure_faults_enumerated(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, bool include_clock_faults,
    bool include_reset_faults, bool collapsing,
    const std::string& unsupported_policy);

SolveFaultResult solve_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy);

bool verify_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy);

std::vector<ProgressiveDetection> simulate_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy);

std::vector<int64_t> simulate_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy);

// ---- Transition model (combinational broadside two-pattern) ----------------
// Separate entry points so the stuck-at path stays byte-identical. Enumeration
// is shared: STR/STF reuse the SA0/SA1 fault rows (the model is a campaign-level
// property, not stored per fault).

SolveTransitionResult solve_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy);

bool verify_transition_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::string& unsupported_policy);

std::vector<ProgressiveDetection> simulate_transition_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<VectorPair>& new_pairs,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy);

std::vector<int64_t> simulate_transition_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy);

}  // namespace faultflow::atpg
