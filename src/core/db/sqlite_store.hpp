#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

namespace faultflow::db {

struct CoverageSummary {
  int64_t total_raw_faults = 0;
  int64_t denominator = 0;
  int64_t detected = 0;
  int64_t undetected = 0;
  int64_t collapsed = 0;
  int64_t excluded_blackbox = 0;
  int64_t excluded_clock = 0;
  int64_t excluded_reset = 0;
  double coverage_percent = 0.0;
};

void init_database(const std::string& db_path);

int64_t start_run(const std::string& db_path, const std::string& vector_source,
                  int64_t vector_count);

void write_vectors(const std::string& db_path, int64_t run_id,
                   const std::string& source,
                   const std::vector<std::string>& patterns);

void write_faults(const std::string& db_path, int64_t run_id,
                  const NormalizedGraph& ng, const CompiledSimGraph& cg,
                  const std::vector<CompactFault>& faults);

CoverageSummary summarize(const std::string& db_path);

void complete_run(const std::string& db_path, int64_t run_id,
                  double coverage_percent);

}  // namespace faultflow::db
