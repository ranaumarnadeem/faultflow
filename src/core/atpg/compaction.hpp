#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace faultflow::atpg {

// Simulate one fully-specified test vector against the given fault ids and
// return the subset it detects. Unlike simulate_tentative_detections (which
// loads faults with skip_protocol_unresolved=true and therefore skips any fault
// whose status != UNDETECTED), this loader is status-agnostic: it considers any
// fault that is not excluded and not collapsed. That is exactly what test-set
// compaction needs, because the faults we compact against are already
// status='detected' after the ATPG run. No DB writes.
std::vector<int64_t> detect_with_vector_unfiltered(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& db_path, const std::map<std::string, bool>& vector,
    const std::vector<std::string>& input_order,
    const std::vector<int64_t>& fault_ids,
    const std::string& unsupported_policy);

}  // namespace faultflow::atpg
