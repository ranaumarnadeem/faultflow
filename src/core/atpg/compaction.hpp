#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "common/types.hpp"

namespace faultflow::atpg {

// A fault already resolved to its compiled net index and stuck-at type --
// mirrors the (net_index, fault_type) pair convention
// simulate_scan_protocol_faults_py already uses. Callers batch-load these
// ONCE from the DB and reuse the same list across many detect_with_vector /
// detect_with_pair calls, so the hot loop never touches the database.
struct FaultTarget {
  int64_t fault_id = 0;
  uint32_t net_index = 0;
  FaultType type = FaultType::SA0;
};

// Simulate one fully-specified test vector against pre-resolved fault targets
// and return the subset of fault ids it detects. No DB access at all -- the
// caller is responsible for having already filtered targets to the faults it
// cares about (e.g. status='detected', exclusion='none', collapsed_into IS
// NULL for compaction). Use this instead of detect_with_vector_unfiltered
// whenever the same target set (or a shrinking subset of it) is queried
// across many vectors in a loop, such as reverse-order compaction -- reusing
// one preloaded target list avoids a fresh DB connection and re-fetch of the
// full remaining-fault set on every single vector.
std::vector<int64_t> detect_with_vector(
    const std::string& json_path, const std::string& cell_map_path,
    const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<FaultTarget>& targets,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// Two-frame (transition) analogue of detect_with_vector: simulate one
// launch/capture PAIR against pre-resolved fault targets via the qualified
// two-frame transition sim. No DB access. See detect_with_vector for when to
// prefer this over detect_with_pair_unfiltered.
std::vector<int64_t> detect_with_pair(
    const std::string& json_path, const std::string& cell_map_path,
    const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<FaultTarget>& targets,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// Simulate one fully-specified test vector against the given fault ids and
// return the subset it detects. Unlike simulate_tentative_detections (which
// loads faults with skip_protocol_unresolved=true and therefore skips any fault
// whose status != UNDETECTED), this loader is status-agnostic: it considers any
// fault that is not excluded and not collapsed. That is exactly what test-set
// compaction needs, because the faults we compact against are already
// status='detected' after the ATPG run. Convenience wrapper around
// detect_with_vector that loads targets from the DB itself -- fine for a
// single call, but callers that loop over many vectors against the same (or a
// shrinking) fault set should load targets once and call detect_with_vector
// directly instead (see FaultTarget's docstring). No DB writes.
std::vector<int64_t> detect_with_vector_unfiltered(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

// Two-frame (transition) analogue of detect_with_vector_unfiltered: simulate one
// launch/capture PAIR against the given fault ids via the qualified two-frame
// transition sim and return the subset it detects. Same status-agnostic loader
// (skip only excluded/collapsed). Coverage-preserving for transition compaction
// because it uses the same engine that detected the faults. Convenience
// wrapper around detect_with_pair -- see detect_with_vector_unfiltered's
// docstring for when to prefer the preloaded form instead. No DB writes.
std::vector<int64_t> detect_with_pair_unfiltered(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& launch,
    const std::map<std::string, bool>& capture,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances = {});

}  // namespace faultflow::atpg
