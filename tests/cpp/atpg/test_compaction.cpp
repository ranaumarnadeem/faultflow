#include <catch2/catch_test_macros.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "atpg/compaction.hpp"
#include "atpg/progressive_atpg.hpp"
#include "db/sqlite_store.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

std::filesystem::path comp_db_path(const std::string& name) {
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

// Insert faults on tiny_inv's Y net that are already status='detected' (the
// state every fault is in by the time compaction runs after an ATPG campaign).
std::vector<int64_t> insert_detected_y_faults(const std::string& path,
                                              int64_t campaign_id, int count) {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int64_t y_compiled = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  std::vector<int64_t> ids;
  ids.reserve(static_cast<size_t>(count));
  for (int i = 0; i < count; ++i) {
    SQLite::Statement q(
        db,
        "INSERT INTO faults(campaign_id, fault_site_key, net_id, net_name, "
        "node_id, compiled_net_index, type, fault_type, status, excluded, "
        "exclusion) VALUES (?, ?, ?, ?, -1, ?, 'sa0', 'sa0', 'detected', 0, "
        "'none')");
    q.bind(1, campaign_id);
    q.bind(2, "site:" + std::to_string(i));
    q.bind(3, i);
    q.bind(4, "n" + std::to_string(i));
    q.bind(5, y_compiled);
    q.exec();
    ids.push_back(db.getLastInsertRowid());
  }
  return ids;
}

std::map<std::string, bool> detecting_vector() { return {{"A", false}}; }

}  // namespace

// This is the showstopper guard: compaction runs after an ATPG campaign, when
// every target fault is status='detected'. The shared hot-path loader skips any
// fault with status != UNDETECTED, so the compaction primitive needs its own
// status-agnostic loader or it would see nothing and drop every vector.
TEST_CASE("detect_with_vector_unfiltered sees status='detected' faults",
          "[atpg][compaction]") {
  const auto path = comp_db_path("faultflow_compaction_unfiltered.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const std::vector<int64_t> fault_ids =
      insert_detected_y_faults(path.string(), campaign_id, 5);

  const std::vector<int64_t> seen = detect_with_vector_unfiltered(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      detecting_vector(), {"A"}, fault_ids, "fail");
  REQUIRE(seen.size() == fault_ids.size());

  // The shared loader skips status != UNDETECTED, which is exactly why the
  // unfiltered primitive exists. This documents the contract difference.
  const std::vector<int64_t> tentative = simulate_tentative_detections(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      detecting_vector(), {"A"}, fault_ids, "fail");
  REQUIRE(tentative.empty());

  std::filesystem::remove(path);
}

// A non-detecting vector (A=true keeps Y high, so Y sa0 is not observed) must
// return no detections even though the faults are present and unfiltered.
TEST_CASE("detect_with_vector_unfiltered returns empty for a non-detecting vector",
          "[atpg][compaction]") {
  const auto path = comp_db_path("faultflow_compaction_nondetect.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const std::vector<int64_t> fault_ids =
      insert_detected_y_faults(path.string(), campaign_id, 3);

  const std::vector<int64_t> seen = detect_with_vector_unfiltered(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      {{"A", true}}, {"A"}, fault_ids, "fail");
  REQUIRE(seen.empty());

  std::filesystem::remove(path);
}

// detect_with_vector (preloaded targets, no DB access at all) must agree
// exactly with detect_with_vector_unfiltered given equivalent inputs -- this
// is the function reverse-order compaction now calls in its per-vector loop
// instead of re-opening the DB and re-fetching the whole remaining-fault set
// on every single vector.
TEST_CASE("detect_with_vector matches detect_with_vector_unfiltered given "
          "equivalent preloaded targets",
          "[atpg][compaction]") {
  const auto path = comp_db_path("faultflow_compaction_preloaded.sqlite");
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const std::vector<int64_t> fault_ids =
      insert_detected_y_faults(path.string(), campaign_id, 5);

  const std::vector<int64_t> via_db = detect_with_vector_unfiltered(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), path.string(),
      detecting_vector(), {"A"}, fault_ids, "fail");
  REQUIRE(via_db.size() == fault_ids.size());

  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const uint32_t y_compiled = static_cast<uint32_t>(
      cg.yosys_to_compiled.at(pg.net_id_by_name("Y")));
  std::vector<FaultTarget> targets;
  targets.reserve(fault_ids.size());
  for (int64_t fault_id : fault_ids) {
    targets.push_back({fault_id, y_compiled, FaultType::SA0});
  }
  const std::vector<int64_t> via_preloaded = detect_with_vector(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(),
      detecting_vector(), {"A"}, targets, "fail");

  REQUIRE(via_preloaded.size() == via_db.size());
  REQUIRE(std::vector<int64_t>(via_preloaded.begin(), via_preloaded.end()) ==
          via_db);

  std::filesystem::remove(path);
}

// Preloaded, no-DB path must also correctly return empty for a non-detecting
// vector -- same shape as the unfiltered non-detecting regression above.
TEST_CASE("detect_with_vector returns empty for a non-detecting vector",
          "[atpg][compaction]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const uint32_t y_compiled = static_cast<uint32_t>(
      cg.yosys_to_compiled.at(pg.net_id_by_name("Y")));
  const std::vector<FaultTarget> targets = {{1, y_compiled, FaultType::SA0},
                                            {2, y_compiled, FaultType::SA0}};

  const std::vector<int64_t> seen = detect_with_vector(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(),
      {{"A", true}}, {"A"}, targets, "fail");
  REQUIRE(seen.empty());
}

// detect_with_vector with an empty target list must short-circuit to empty
// without touching the graph cache or crashing on an empty batch.
TEST_CASE("detect_with_vector returns empty given no targets",
          "[atpg][compaction]") {
  const std::vector<int64_t> seen = detect_with_vector(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(),
      detecting_vector(), {"A"}, {}, "fail");
  REQUIRE(seen.empty());
}
