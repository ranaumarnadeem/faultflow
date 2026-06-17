#include <catch2/catch_test_macros.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "db/sqlite_store.hpp"
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

int64_t insert_test_campaign(const std::string& path) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  db.exec("PRAGMA foreign_keys = ON");
  db.exec(R"sql(
INSERT INTO campaigns(
  campaign_type, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
  yosys_version, faultflow_version, collapsing, unsupported_cells,
  include_clock_faults, include_reset_faults
) VALUES (
  'comb', 'demo', 'net', 'cell', 'cfg', 'tmpl', 'yosys', 'pipeline-v1',
  0, 'fail', 0, 0
)
)sql");
  return db.getLastInsertRowid();
}

CompactFault fault(uint32_t net_index, FaultType type, FaultStatus status) {
  CompactFault f;
  f.net_index = net_index;
  f.type = type;
  f.status = status;
  return f;
}

// Inserts `count` distinct fault rows (distinct site keys/nets) in one
// transaction and returns their ids in insertion order.
std::vector<int64_t> insert_fault_rows(const std::string& path,
                                       int64_t campaign_id, int count) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  SQLite::Transaction txn(db);
  SQLite::Statement q(
      db,
      "INSERT INTO faults(campaign_id, fault_site_key, net_id, net_name, "
      "node_id, compiled_net_index, type, fault_type, status, excluded, "
      "exclusion) VALUES (?, ?, ?, ?, -1, ?, ?, ?, 'undetected', 'none', "
      "'none')");
  std::vector<int64_t> ids;
  ids.reserve(static_cast<size_t>(count));
  for (int i = 0; i < count; ++i) {
    const std::string type = (i % 2 == 0) ? "sa0" : "sa1";
    q.bind(1, campaign_id);
    q.bind(2, "site:" + std::to_string(i));
    q.bind(3, i);
    q.bind(4, "n" + std::to_string(i));
    q.bind(5, i);
    q.bind(6, type);
    q.bind(7, type);
    q.exec();
    ids.push_back(db.getLastInsertRowid());
    q.reset();
  }
  txn.commit();
  return ids;
}

void require_records_equal(const db::FaultRecord& a, const db::FaultRecord& b) {
  REQUIRE(a.id == b.id);
  REQUIRE(a.campaign_id == b.campaign_id);
  REQUIRE(a.compiled_net_index == b.compiled_net_index);
  REQUIRE(a.type == b.type);
  REQUIRE(a.status == b.status);
  REQUIRE(a.exclusion == b.exclusion);
  REQUIRE(a.collapsed_into == b.collapsed_into);
  REQUIRE(a.protocol_unresolved == b.protocol_unresolved);
}

}  // namespace

TEST_CASE("SQLite store initializes schema", "[db]") {
  const auto path = db_path("faultflow_sqlite_store_test.sqlite");

  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());
  const db::CoverageSummary s = db::summarize(path.string(), campaign_id);

  REQUIRE(s.total_raw_faults == 0);
  REQUIRE(s.denominator == 0);
  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  REQUIRE(has_column(sqlite, "campaigns", "netlist_hash"));
  REQUIRE(has_column(sqlite, "campaigns", "yosys_version"));
  REQUIRE(has_column(sqlite, "faults", "fault_site_key"));
  REQUIRE(has_column(sqlite, "faults", "protocol_unresolved"));
  REQUIRE(has_column(sqlite, "runs", "campaign_id"));
  REQUIRE(has_column(sqlite, "vectors", "campaign_id"));
  std::filesystem::remove(path);
}

TEST_CASE("SQLite store freezes run initial FF state", "[db][sequential]") {
  const auto path = db_path("faultflow_sqlite_store_initial_ff.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());
  const int64_t run_id = db::start_run(path.string(), campaign_id,
                                       "seq_vectors.json", 3, "{\"0\":true}");

  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(sqlite,
                      "SELECT initial_ff_state FROM runs WHERE id = ?");
  q.bind(1, run_id);
  REQUIRE(q.executeStep());
  REQUIRE(q.getColumn(0).getString() == "{\"0\":true}");
  std::filesystem::remove(path);
}

TEST_CASE("SQLite store writes detections and excludes collapsed faults", "[db]") {
  const auto path = db_path("faultflow_sqlite_store_write_test.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());
  const int64_t run_id =
      db::start_run(path.string(), campaign_id, "vectors.test", 2);
  db::write_vectors(path.string(), campaign_id, run_id, "vectors.test",
                    {"00", "11"});

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

  db::write_faults(path.string(), campaign_id, run_id, ng, cg, faults);
  const db::CoverageSummary s = db::summarize(path.string(), campaign_id);

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

TEST_CASE("SQLite store fault writes roll back on insertion failure", "[db]") {
  const auto path = db_path("faultflow_sqlite_store_rollback_test.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());
  const int64_t run_id =
      db::start_run(path.string(), campaign_id, "vectors.test", 1);
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  const auto original = fault(0, FaultType::SA0, FaultStatus::UNDETECTED);
  db::write_faults(path.string(), campaign_id, run_id, ng, cg, {original});

  REQUIRE_THROWS_AS(
      db::write_faults(path.string(), campaign_id, run_id, ng, cg,
                       {original, original}),
      std::exception);

  SQLite::Database sqlite(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(sqlite, "SELECT COUNT(*) FROM faults");
  REQUIRE(q.executeStep());
  REQUIRE(q.getColumn(0).getInt() == 1);
  std::filesystem::remove(path);
}

TEST_CASE("load_faults matches per-id load_fault", "[db]") {
  const auto path = db_path("faultflow_load_faults_match.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());

  const std::vector<int64_t> ids =
      insert_fault_rows(path.string(), campaign_id, 5);
  // Vary the mutable fields so decoding is exercised, not just defaults.
  db::mark_fault_redundant(path.string(), ids[1], "model-x");
  db::mark_fault_protocol_unresolved(path.string(), ids[2]);

  std::vector<int64_t> query_ids = ids;
  const int64_t missing_id = ids.back() + 1000;
  query_ids.push_back(missing_id);

  const std::map<int64_t, db::FaultRecord> batch =
      db::load_faults(path.string(), query_ids);

  REQUIRE(batch.size() == ids.size());  // missing id omitted, not invented
  REQUIRE(batch.find(missing_id) == batch.end());
  for (int64_t id : ids) {
    const auto it = batch.find(id);
    REQUIRE(it != batch.end());
    require_records_equal(it->second, db::load_fault(path.string(), id));
  }
  REQUIRE_THROWS(db::load_fault(path.string(), missing_id));
  std::filesystem::remove(path);
}

TEST_CASE("load_faults chunks beyond the SQLite variable limit", "[db]") {
  const auto path = db_path("faultflow_load_faults_chunk.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_test_campaign(path.string());

  const int kCount = 950;  // > 900-id chunk size: forces multiple IN queries
  const std::vector<int64_t> ids =
      insert_fault_rows(path.string(), campaign_id, kCount);

  const std::map<int64_t, db::FaultRecord> batch =
      db::load_faults(path.string(), ids);

  REQUIRE(batch.size() == static_cast<size_t>(kCount));
  for (int64_t id : ids) {
    REQUIRE(batch.find(id) != batch.end());
  }
  // A record from a later chunk must decode identically to the single loader.
  require_records_equal(batch.at(ids.back()),
                        db::load_fault(path.string(), ids.back()));
  std::filesystem::remove(path);
}
