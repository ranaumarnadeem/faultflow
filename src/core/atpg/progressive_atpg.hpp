#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

namespace faultflow::atpg {

struct ProgressiveDetection {
  int64_t fault_id = 0;
  int64_t vector_index = 0;

  // Value equality so tests can assert the parallel grading result is
  // bit-identical to the serial one (same detections in the same order).
  bool operator==(const ProgressiveDetection& other) const {
    return fault_id == other.fault_id && vector_index == other.vector_index;
  }
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
    const std::string& test_mode = "", bool cone_restrict = true,
    bool incremental = false);

bool verify_fault_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t fault_id,
    const std::map<std::string, bool>& vector,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "");

// `sim_threads` parallelizes fault grading: <= 0 means auto (hardware
// concurrency), 1 is serial, N uses N threads. The result is bit-identical for
// any thread count (DB writes + merge happen serially after join).
std::vector<ProgressiveDetection> simulate_incremental(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, int64_t campaign_id, int64_t run_id,
    const std::vector<std::map<std::string, bool>>& new_vectors,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids, int64_t vector_start_index,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "", int sim_threads = 1);

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
    const std::vector<std::string>& blackbox_instances = {},
    bool cone_restrict = true);

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
    const std::vector<std::string>& blackbox_instances = {},
    int sim_threads = 1);

std::vector<int64_t> simulate_transition_tentative_detections(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// Tentative simulation using pre-loaded fault records (no DB round-trips).
// Each entry in `preloaded` is (fault_id, compiled_net_index, type: 0=SA0/1=SA1).
// Records already known to be excluded/collapsed are excluded by the caller
// (_active_fault_rows filters them); this function runs sim on all provided records.
std::vector<int64_t> simulate_tentative_from_preloaded(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::tuple<int64_t, uint32_t, uint8_t>>& preloaded,
    const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    const std::string& test_mode = "", int sim_threads = 1);

std::vector<int64_t> simulate_transition_tentative_from_preloaded(
    const std::string& json_path, const std::string& cell_map_path,
    const std::vector<std::tuple<int64_t, uint32_t, uint8_t>>& preloaded,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {},
    int sim_threads = 1);

// Real (non-pseudo-PI) top-level input PIs that must be held constant across
// the launch/capture frames of a single scan two-frame transition test --
// shared by both the LOC (build_scan_loc_view, this file) and LOS
// (solve_scan_los_transition_fault_for_db, this file) views, since a held
// input is held for the exact same physical reason (a tester cannot change a
// non-scan-driven input's value between the launch and capture edges of one
// at-speed capture window) regardless of which scan protocol drives capture.
// Exposed (not file-local) so its per-bit handling of a multi-bit port is
// directly unit-testable.
std::vector<uint32_t> held_real_pis(const ParsedGraph& parsed,
                                    const CompiledSimGraph& cg);

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
    const std::vector<std::string>& blackbox_instances = {},
    bool cone_restrict = true);

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
    const std::vector<std::string>& blackbox_instances = {},
    bool cone_restrict = true);

}  // namespace faultflow::atpg
