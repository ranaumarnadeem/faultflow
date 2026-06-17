#include <catch2/catch_test_macros.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "atpg/progressive_atpg.hpp"
#include "db/sqlite_store.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

std::filesystem::path db_path(const std::string& name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  std::filesystem::remove(path);
  return path;
}

int64_t insert_campaign(const std::string& path) {
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

int64_t insert_fault(const std::string& path, int64_t campaign_id,
                     int64_t compiled_net, const std::string& type,
                     int ordinal) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  SQLite::Statement q(
      db,
      "INSERT INTO faults(campaign_id, fault_site_key, net_id, net_name, "
      "node_id, compiled_net_index, type, fault_type, status, excluded, "
      "exclusion) VALUES (?, ?, ?, ?, -1, ?, ?, ?, 'undetected', 'none', "
      "'none')");
  q.bind(1, campaign_id);
  q.bind(2, "site:" + std::to_string(ordinal));
  q.bind(3, ordinal);
  q.bind(4, "n" + std::to_string(ordinal));
  q.bind(5, compiled_net);
  q.bind(6, type);
  q.bind(7, type);
  q.exec();
  return db.getLastInsertRowid();
}

std::vector<int64_t> insert_duplicate_detectable_faults(
    const std::string& path, int64_t campaign_id, int count) {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int64_t y_compiled = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  std::vector<int64_t> ids;
  ids.reserve(static_cast<size_t>(count));
  for (int i = 0; i < count; ++i) {
    ids.push_back(insert_fault(path, campaign_id, y_compiled, "sa0", i));
  }
  return ids;
}

std::map<std::string, bool> detecting_tiny_inv_vector() {
  return {{"A", false}};
}

}  // namespace

TEST_CASE("simulate_tentative_detections uses packed batches", "[atpg][pfs]") {
  const auto path = db_path("faultflow_tentative_packed.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const std::vector<int64_t> fault_ids =
      insert_duplicate_detectable_faults(path.string(), campaign_id, 70);

  reset_simulation_instrumentation();
  const std::vector<int64_t> detected = simulate_tentative_detections(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      detecting_tiny_inv_vector(), {"A"}, fault_ids, "fail");
  const SimulationInstrumentation stats = simulation_instrumentation();

  REQUIRE(detected.size() == 70);
  REQUIRE(stats.single_fault_calls == 0);
  REQUIRE(stats.batch_fault_calls == 2);
  REQUIRE(stats.load_graph_calls == 1);
  std::filesystem::remove(path);
}

TEST_CASE("simulate_incremental records earliest vector and batches partial tail",
          "[atpg][pfs]") {
  const auto path = db_path("faultflow_incremental_packed.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const int64_t run_id = db::start_run(path.string(), campaign_id, "native", 2);
  const std::vector<int64_t> fault_ids =
      insert_duplicate_detectable_faults(path.string(), campaign_id, 64);
  const std::vector<std::map<std::string, bool>> vectors = {
      {{"A", true}},
      {{"A", false}},
  };

  reset_simulation_instrumentation();
  const std::vector<ProgressiveDetection> detections = simulate_incremental(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      campaign_id, run_id, vectors, {"A"}, fault_ids, 10, "fail");
  const SimulationInstrumentation stats = simulation_instrumentation();

  REQUIRE(detections.size() == 64);
  REQUIRE(stats.single_fault_calls == 0);
  REQUIRE(stats.batch_fault_calls == 4);
  REQUIRE(stats.load_graph_calls == 1);
  for (const ProgressiveDetection& detection : detections) {
    REQUIRE(detection.vector_index == 11);
  }
  SQLite::Database db(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(db,
                      "SELECT COUNT(*) FROM faults WHERE status = 'detected' "
                      "AND detected_by_vector = 11");
  REQUIRE(q.executeStep());
  REQUIRE(q.getColumn(0).getInt() == 64);
  std::filesystem::remove(path);
}

TEST_CASE("graph cache builds each netlist once across repeated sim calls",
          "[atpg][pfs]") {
  const auto path = db_path("faultflow_graph_cache_collapse.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const std::vector<int64_t> fault_ids =
      insert_duplicate_detectable_faults(path.string(), campaign_id, 8);

  reset_simulation_instrumentation();
  for (int i = 0; i < 3; ++i) {
    const std::vector<int64_t> detected = simulate_tentative_detections(
        test::fixture_path("tiny_inv.json"), test::cell_map_path(),
        path.string(), detecting_tiny_inv_vector(), {"A"}, fault_ids, "fail");
    REQUIRE(detected.size() == 8);
  }
  // Three sim calls on one netlist must parse/normalize/compile the graph
  // exactly once; the shared content-keyed cache serves the rest.
  REQUIRE(simulation_instrumentation().load_graph_calls == 1);
  std::filesystem::remove(path);
}
