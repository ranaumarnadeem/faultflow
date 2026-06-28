#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow::db {

constexpr int kSchemaUserVersion = 6;

struct CoverageSummary {
  int64_t total_raw_faults = 0;
  int64_t denominator = 0;
  int64_t detected = 0;
  int64_t undetected = 0;
  int64_t redundant = 0;
  int64_t collapsed = 0;
  int64_t excluded_blackbox = 0;
  int64_t excluded_clock = 0;
  int64_t excluded_reset = 0;
  int64_t excluded_scan = 0;
  int64_t excluded_scan_internal = 0;
  int64_t excluded_scan_chain = 0;
  double coverage_percent = 0.0;
};

struct AtpgRunStats {
  std::string terminal_reason;
  int rounds = 0;
  int sat = 0;
  int unsat = 0;
  int timeout = 0;
  int unknown = 0;
  int rejected_candidates = 0;
  int generated_vectors = 0;
  int accepted_vectors = 0;
};

struct FaultRecord {
  int64_t id = 0;
  int64_t campaign_id = 0;
  uint32_t compiled_net_index = 0;
  FaultType type = FaultType::SA0;
  FaultStatus status = FaultStatus::UNDETECTED;
  FaultExclusion exclusion = FaultExclusion::NONE;
  uint32_t collapsed_into = UINT32_MAX;
  bool protocol_unresolved = false;
};

void init_database(const std::string& db_path);

int64_t fault_count(const std::string& db_path, int64_t campaign_id);

int64_t start_run(const std::string& db_path, int64_t campaign_id,
                  const std::string& vector_source, int64_t vector_count,
                  const std::string& initial_ff_state = "all_zero");

// `patterns` is the sampled (capture) frame. `launch_patterns`, when non-empty,
// is the parallel launch frame for transition vectors (must match `patterns`
// length); empty leaves launch_pattern='' (single-frame stuck-at).
void write_vectors(const std::string& db_path, int64_t campaign_id,
                   int64_t run_id, const std::string& source,
                   const std::vector<std::string>& patterns,
                   const std::vector<std::string>& launch_patterns = {});

void append_vectors(const std::string& db_path, int64_t campaign_id,
                    int64_t run_id, const std::string& source,
                    const std::vector<std::string>& patterns,
                    int64_t start_index,
                    const std::vector<std::string>& launch_patterns = {});

void write_faults(const std::string& db_path, int64_t campaign_id,
                  int64_t run_id, const NormalizedGraph& ng,
                  const CompiledSimGraph& cg,
                  const std::vector<CompactFault>& faults);

void insert_faults_if_empty(const std::string& db_path, int64_t campaign_id,
                            const NormalizedGraph& ng,
                            const CompiledSimGraph& cg,
                            const std::vector<CompactFault>& faults);

FaultRecord load_fault(const std::string& db_path, int64_t fault_id);

// Batch variant of load_fault: one connection + chunked "id IN (...)" queries
// instead of a fresh connection per id. Returns a map keyed by fault id; ids
// absent from the DB are simply absent from the map (callers decide how to
// treat a miss). Decoding matches load_fault column-for-column.
std::map<int64_t, FaultRecord> load_faults(
    const std::string& db_path, const std::vector<int64_t>& fault_ids);

void mark_fault_detected(const std::string& db_path, int64_t campaign_id,
                         int64_t run_id, int64_t fault_id,
                         int64_t vector_index);

// Batched form of mark_fault_detected: opens the DB and verifies the schema
// once, then marks every fault in one transaction (reusing two prepared
// statements). Row-for-row identical to calling mark_fault_detected per id,
// but avoids a DB open + commit per fault when one vector detects many faults.
void mark_faults_detected(const std::string& db_path, int64_t campaign_id,
                          int64_t run_id, int64_t vector_index,
                          const std::vector<int64_t>& fault_ids);

void mark_fault_redundant(const std::string& db_path, int64_t fault_id,
                          const std::string& redundancy_model_id);

void mark_fault_protocol_unresolved(const std::string& db_path,
                                    int64_t fault_id);

void invalidate_stale_redundant(const std::string& db_path,
                                int64_t campaign_id,
                                const std::string& redundancy_model_id);

void update_run_vector_count(const std::string& db_path, int64_t run_id,
                             int64_t vector_count);

CoverageSummary summarize(const std::string& db_path, int64_t campaign_id);

void complete_run(const std::string& db_path, int64_t run_id,
                  double coverage_percent);

void complete_run_with_atpg(const std::string& db_path, int64_t run_id,
                            double coverage_percent, const AtpgRunStats& stats);

}  // namespace faultflow::db
