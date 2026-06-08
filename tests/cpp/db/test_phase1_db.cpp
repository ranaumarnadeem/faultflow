#include <catch2/catch_test_macros.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <stdexcept>

#include "db/phase1_db.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;

namespace {

std::filesystem::path db_path(const std::string& name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  std::filesystem::remove(path);
  return path;
}

bool has_column(SQLite::Database& db, const std::string& table,
                const std::string& column) {
  SQLite::Statement q(db, "PRAGMA table_info(" + table + ")");
  while (q.executeStep()) {
    if (q.getColumn(1).getString() == column) {
      return true;
    }
  }
  return false;
}

CompactFault fault(uint32_t net_index, FaultType type, FaultStatus status) {
  CompactFault f;
  f.net_index = net_index;
  f.type = type;
  f.status = status;
  return f;
}

}  // namespace

TEST_CASE("Phase1 DB initializes schema", "[phase1][db]") {
  const auto path = db_path("faultflow_phase1_db_test.sqlite");

  db::init_database(path.string());
  const db::CoverageSummary s = db::summarize(path.string());

  REQUIRE(s.total_raw_faults == 0);
  REQUIRE(s.denominator == 0);
  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  REQUIRE(has_column(sqlite, "design_fingerprint", "netlist_hash"));
  REQUIRE(has_column(sqlite, "design_fingerprint", "yosys_version"));
  REQUIRE(has_column(sqlite, "faults", "net_name"));
  REQUIRE(has_column(sqlite, "faults", "node_id"));
  REQUIRE(has_column(sqlite, "faults", "type"));
  REQUIRE(has_column(sqlite, "faults", "excluded"));
  REQUIRE(has_column(sqlite, "faults", "collapsed_to"));
  REQUIRE(has_column(sqlite, "vectors", "inputs"));
  REQUIRE(has_column(sqlite, "vectors", "expected"));
  REQUIRE(has_column(sqlite, "vectors", "verified"));
  std::filesystem::remove(path);
}

TEST_CASE("Phase1 DB writes detections and excludes collapsed faults",
          "[phase1][db]") {
  const auto path = db_path("faultflow_phase1_db_write_test.sqlite");
  db::init_database(path.string());
  const int64_t run_id = db::start_run(path.string(), "vectors.test", 2);
  db::write_vectors(path.string(), run_id, "vectors.test", {"00", "11"});

  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
  REQUIRE(cg.compiled_to_yosys.size() >= 3);

  std::vector<CompactFault> faults;
  auto detected = fault(0, FaultType::SA0, FaultStatus::DETECTED);
  detected.detected_by_vector = 1;
  faults.push_back(detected);
  faults.push_back(fault(1, FaultType::SA1, FaultStatus::UNDETECTED));
  auto collapsed = fault(2, FaultType::SA0, FaultStatus::UNDETECTED);
  collapsed.collapsed_into = 0;
  faults.push_back(collapsed);
  auto clock = fault(0, FaultType::SA1, FaultStatus::UNDETECTED);
  clock.exclusion = FaultExclusion::CLOCK;
  faults.push_back(clock);

  db::write_faults(path.string(), run_id, ng, cg, faults);
  const db::CoverageSummary s = db::summarize(path.string());

  REQUIRE(s.total_raw_faults == 4);
  REQUIRE(s.denominator == 2);
  REQUIRE(s.detected == 1);
  REQUIRE(s.undetected == 1);
  REQUIRE(s.collapsed == 1);
  REQUIRE(s.excluded_clock == 1);

  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(sqlite, "SELECT COUNT(*) FROM fault_detections");
  REQUIRE(q.executeStep());
  REQUIRE(q.getColumn(0).getInt() == 1);
  std::filesystem::remove(path);
}

TEST_CASE("Phase1 DB fault writes roll back on insertion failure",
          "[phase1][db]") {
  const auto path = db_path("faultflow_phase1_db_rollback_test.sqlite");
  db::init_database(path.string());
  const int64_t run_id = db::start_run(path.string(), "vectors.test", 1);
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  const auto original = fault(0, FaultType::SA0, FaultStatus::UNDETECTED);
  db::write_faults(path.string(), run_id, ng, cg, {original});

  REQUIRE_THROWS_AS(
      db::write_faults(path.string(), run_id, ng, cg, {original, original}),
      std::exception);

  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(sqlite, "SELECT COUNT(*) FROM faults");
  REQUIRE(q.executeStep());
  REQUIRE(q.getColumn(0).getInt() == 1);
  std::filesystem::remove(path);
}
