// Parallel fault-grading determinism (Seam 1: stuck-at simulate_incremental).
//
// The hard contract: grading the SAME work over `sim_threads` disjoint slices
// must produce a result that is BIT-IDENTICAL to the serial (sim_threads == 1)
// path for any thread count — same detected-id/vector-index list in the same
// order, same DB detected-row count, and a batch_fault_calls total that is
// independent of the thread count. Serial is already cross-checked against
// GoldenRefSim elsewhere, so serial == parallel transitively pins parallel to
// the golden oracle.
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

std::filesystem::path fresh_db(const std::string& name) {
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

struct Setup {
  std::filesystem::path path;
  int64_t campaign_id = 0;
  int64_t run_id = 0;
  std::vector<int64_t> fault_ids;
};

// Build a fresh DB with `count` faults on tiny_inv's Y net (Y = ~A). Every
// third fault is sa1, the rest sa0. With the two-vector run {A=1},{A=0}:
//   A=1 -> Y=0: sa1 (force 1) is detected, sa0 (force 0 == golden) is not.
//   A=0 -> Y=1: sa0 (force 0) is detected, sa1 (force 1 == golden) is not.
// So EVERY fault is detected in exactly one frame, split across both vectors —
// exercising the cross-vector carry-forward, the still_active merge order, and
// a per-fault first-detection vector_index that depends only on the frame, not
// on which slice graded it.
Setup make_setup(const std::string& name, int count) {
  const auto path = fresh_db(name);
  db::init_database(path.string());
  const int64_t campaign_id = insert_campaign(path.string());
  const int64_t run_id = db::start_run(path.string(), campaign_id, "native", 2);
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int64_t y_compiled = cg.yosys_to_compiled.at(pg.net_id_by_name("Y"));
  std::vector<int64_t> ids;
  ids.reserve(static_cast<size_t>(count));
  for (int i = 0; i < count; ++i) {
    const std::string type = (i % 3 == 0) ? "sa1" : "sa0";
    ids.push_back(insert_fault(path.string(), campaign_id, y_compiled, type, i));
  }
  return {path, campaign_id, run_id, ids};
}

struct RunOutcome {
  std::vector<ProgressiveDetection> detections;
  int64_t detected_rows = 0;
  int64_t batch_fault_calls = 0;
};

RunOutcome run_with_threads(const std::string& name, int count, int sim_threads) {
  const Setup s = make_setup(name, count);
  const std::vector<std::map<std::string, bool>> vectors = {
      {{"A", true}},
      {{"A", false}},
  };
  reset_simulation_instrumentation();
  RunOutcome out;
  out.detections = simulate_incremental(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), s.path.string(),
      s.campaign_id, s.run_id, vectors, {"A"}, s.fault_ids, 10, "fail", {}, "",
      sim_threads);
  out.batch_fault_calls = simulation_instrumentation().batch_fault_calls;
  SQLite::Database db(s.path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(
      db, "SELECT COUNT(*) FROM faults WHERE status = 'detected'");
  REQUIRE(q.executeStep());
  out.detected_rows = q.getColumn(0).getInt64();
  std::filesystem::remove(s.path);
  return out;
}

// Transition twin: two launch/capture pairs over tiny_inv's Y. Pair 0 (A:1->0)
// makes Y rise 0->1 and detects the slow-to-rise (sa0) faults; pair 1 (A:0->1)
// makes Y fall 1->0 and detects the slow-to-fall (sa1) faults. So the mixed
// sa0/sa1 fault list is fully detected, split across both pairs.
RunOutcome run_transition_with_threads(const std::string& name, int count,
                                       int sim_threads) {
  const Setup s = make_setup(name, count);
  const std::vector<
      std::pair<std::map<std::string, bool>, std::map<std::string, bool>>>
      pairs = {
          {{{"A", true}}, {{"A", false}}},
          {{{"A", false}}, {{"A", true}}},
      };
  reset_simulation_instrumentation();
  RunOutcome out;
  out.detections = simulate_transition_incremental(
      test::fixture_path("tiny_inv.json"), test::cell_map_path(), s.path.string(),
      s.campaign_id, s.run_id, pairs, {"A"}, s.fault_ids, 10, "fail", {},
      sim_threads);
  out.batch_fault_calls = simulation_instrumentation().batch_fault_calls;
  SQLite::Database db(s.path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(
      db, "SELECT COUNT(*) FROM faults WHERE status = 'detected'");
  REQUIRE(q.executeStep());
  out.detected_rows = q.getColumn(0).getInt64();
  std::filesystem::remove(s.path);
  return out;
}

}  // namespace

TEST_CASE("parallel simulate_incremental is bit-identical to serial",
          "[atpg][pfs][parallel]") {
  // 511 = 8*63 + 7: many full batches plus a partial tail, so slicing on
  // 63-fault boundaries genuinely splits work and the tail lands in the last
  // non-empty slice.
  const int count = 8 * 63 + 7;
  const RunOutcome serial = run_with_threads("ff_parallel_serial.sqlite", count, 1);

  // Sanity: the serial baseline detects every fault exactly once.
  REQUIRE(serial.detections.size() == static_cast<size_t>(count));
  REQUIRE(serial.detected_rows == count);

  for (const int threads : {2, 4, 8}) {
    const RunOutcome par =
        run_with_threads("ff_parallel_threads.sqlite", count, threads);
    REQUIRE(par.detections == serial.detections);
    REQUIRE(par.detected_rows == serial.detected_rows);
    // One batch is graded exactly once regardless of how slices are cut.
    REQUIRE(par.batch_fault_calls == serial.batch_fault_calls);
  }
}

TEST_CASE("parallel simulate_incremental clamps threads above batch count",
          "[atpg][pfs][parallel]") {
  // Fewer faults than one full batch: only one batch exists, so asking for many
  // threads must clamp to a single slice and still match serial exactly.
  const int count = 5;
  const RunOutcome serial = run_with_threads("ff_parallel_tiny_serial.sqlite", count, 1);
  const RunOutcome par = run_with_threads("ff_parallel_tiny_threads.sqlite", count, 16);
  REQUIRE(par.detections == serial.detections);
  REQUIRE(par.detected_rows == serial.detected_rows);
  REQUIRE(par.batch_fault_calls == serial.batch_fault_calls);
}

TEST_CASE("parallel simulate_transition_incremental is bit-identical to serial",
          "[atpg][pfs][parallel]") {
  const int count = 8 * 63 + 7;
  const RunOutcome serial =
      run_transition_with_threads("ff_parallel_tr_serial.sqlite", count, 1);

  // Both transition pairs together detect every fault exactly once.
  REQUIRE(serial.detections.size() == static_cast<size_t>(count));
  REQUIRE(serial.detected_rows == count);

  for (const int threads : {2, 4, 8}) {
    const RunOutcome par =
        run_transition_with_threads("ff_parallel_tr_threads.sqlite", count, threads);
    REQUIRE(par.detections == serial.detections);
    REQUIRE(par.detected_rows == serial.detected_rows);
    REQUIRE(par.batch_fault_calls == serial.batch_fault_calls);
  }
}
