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
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

SolveFaultResult solve_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "", bool cone_restrict = true);

bool verify_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "");

std::vector<ProgressiveDetection> simulate_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "");

std::vector<int64_t> simulate_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "");

// ---- Transition model (combinational broadside two-pattern) ----------------
// Separate entry points so the stuck-at path stays byte-identical. Enumeration
// is shared: STR/STF reuse the SA0/SA1 fault rows (the model is a campaign-level
// property, not stored per fault).

SolveTransitionResult solve_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

bool verify_transition_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

std::vector<ProgressiveDetection> simulate_transition_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<VectorPair>& new_pairs,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

std::vector<int64_t> simulate_transition_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// ---- Transition model (scan launch-on-capture) -----------------------------
// SAT on the reduced scan ATPG view: the LOC couples (capture PPI == launch PPO)
// and held real PIs are derived from the view's `__ppi_<inst>` / `__ppo_<inst>`
// port pairs, so the binding signature matches the broadside solver. The reduced
// view is combinational (FFs already lowered to pseudo-ports), so the
// combinational-only guard passes. Reduced-view verification reuses
// verify_transition_fault_vector; full-protocol grading runs through the
// two-capture scan-protocol sim on the generic netlist.
SolveTransitionResult solve_scan_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// ---- Transition model (scan launch-on-shift) -------------------------------
// LOS couples (capture PPI == launch predecessor PPI) and chain-head PPIs (free
// scan-in bits) come from the Python chain order, so they are passed in as
// `__ppi_*` PORT-NAME lists: `couple_ports[i] = (capture_ppi, predecessor_ppi)`
// and `head_ppi_ports`. Held real PIs = all non-`__ppi_` inputs (derived here).
SolveTransitionResult solve_scan_los_transition_fault_for_db(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::vector<std::pair<std::string, std::string>>& couple_ports,
    const std::vector<std::string>& head_ppi_ports,
    const std::vector<std::string>& blocked_patterns, int conflict_limit,
    int sat_timeout_seconds, const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

}  // namespace faultflow::atpg
