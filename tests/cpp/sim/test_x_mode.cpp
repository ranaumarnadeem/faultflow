// Stage 2b of the X engine: an X-mode graph (the "+x" graph policy) keeps a
// netlist's x/z bits unknown, the X engine grades through them, and a fault it
// only possibly detects is stored as 'possible' and credited by summarize.
//
// tiny_xbit: y = a & (c | x), x an "x" bit of the netlist.
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "atpg/compaction.hpp"
#include "atpg/progressive_atpg.hpp"
#include "db/sqlite_store.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/xsim/bit_parallel_sim_x.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

using namespace faultflow;

namespace {

const std::string& fixture() {
  static const std::string path = test::fixture_path("tiny_xbit.json");
  return path;
}

int count_type(const CompiledSimGraph& cg, GateType type) {
  int n = 0;
  for (const SimNode& node : cg.nodes) {
    n += node.type == type ? 1 : 0;
  }
  return n;
}

TestVector vector_of(const ParsedGraph& parsed, bool a, bool c) {
  TestVector vec;
  vec.inputs[parsed.net_id_by_name("a")] = a;
  vec.inputs[parsed.net_id_by_name("c")] = c;
  return vec;
}

Logic y_of(const CachedGraph& g, bool a, bool c) {
  const GoldenRefSimX ref;
  const auto samples = ref.simulate_sequence(g.cg, vector_of(g.parsed, a, c), nullptr, {});
  return samples.at(0).at(g.parsed.net_id_by_name("y"));
}

CompactFault stuck(const CachedGraph& g, const std::string& net, FaultType type) {
  CompactFault f;
  f.net_index = static_cast<uint32_t>(g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name(net)));
  f.type = type;
  f.exclusion = FaultExclusion::NONE;
  return f;
}

std::filesystem::path fresh_db(const std::string& name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  std::filesystem::remove(path);
  db::init_database(path.string());
  return path;
}

int64_t insert_campaign(const std::string& path) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  db.exec(R"sql(
INSERT INTO campaigns(
  campaign_type, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
  yosys_version, faultflow_version, collapsing, unsupported_cells,
  include_clock_faults, include_reset_faults
) VALUES (
  'comb', 'tiny_xbit', 'net', 'cell', 'cfg', 'tmpl', 'yosys', 'pipeline-v1',
  0, 'fail', 0, 0
)
)sql");
  return db.getLastInsertRowid();
}

// The campaign's fault id for (compiled net, "sa0"/"sa1").
int64_t fault_id(const std::string& path, int64_t campaign_id, uint32_t net,
                 const std::string& type) {
  SQLite::Database db(path, SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT id FROM faults WHERE campaign_id=? AND "
                          "compiled_net_index=? AND fault_type=?");
  q.bind(1, campaign_id);
  q.bind(2, static_cast<int64_t>(net));
  q.bind(3, type);
  REQUIRE(q.executeStep());
  return q.getColumn(0).getInt64();
}

std::string status_of(const std::string& path, int64_t id) {
  SQLite::Database db(path, SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT status FROM faults WHERE id=?");
  q.bind(1, id);
  REQUIRE(q.executeStep());
  return q.getColumn(0).getString();
}

std::vector<int64_t> all_fault_ids(const std::string& path, int64_t campaign_id) {
  SQLite::Database db(path, SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT id FROM faults WHERE campaign_id=? ORDER BY id");
  q.bind(1, campaign_id);
  std::vector<int64_t> ids;
  while (q.executeStep()) {
    ids.push_back(q.getColumn(0).getInt64());
  }
  return ids;
}

}  // namespace

TEST_CASE("A two-valued graph ties an x bit to 0; an X-mode graph keeps it unknown",
          "[xsim][xmode]") {
  const CachedGraph& two = load_cached_graph(fixture(), test::cell_map_path(), "fail");
  const CachedGraph& xg = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  CHECK_FALSE(two.cg.x_mode);
  CHECK(xg.cg.x_mode);
  CHECK(count_type(two.cg, GateType::CONSTX) == 0);
  CHECK(count_type(xg.cg, GateType::CONSTX) == 1);

  // Two-valued: y = a & c.
  const GoldenRefSim golden;
  const auto y_two = [&](bool a, bool c) {
    return golden.simulate_fault_free(two.cg, vector_of(two.parsed, a, c))
        .at(two.parsed.net_id_by_name("y"));
  };
  CHECK_FALSE(y_two(true, false));
  CHECK(y_two(true, true));

  // X mode: y = a & (c | X).
  CHECK(y_of(xg, true, false) == Logic::X);
  CHECK(y_of(xg, true, true) == Logic::ONE);
  CHECK(y_of(xg, false, false) == Logic::ZERO);
}

TEST_CASE("The X engine sees a fault through an x bit only possibly detected",
          "[xsim][xmode]") {
  const CachedGraph& xg = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  const TestVector vec = vector_of(xg.parsed, true, true);
  const GoldenRefSimX ref;
  const auto good = ref.simulate_sequence(xg.cg, vec, nullptr, {});
  // c stuck at 0: y = a & X, unknown where the good y is 1.
  const CompactFault c_sa0 = stuck(xg, "c", FaultType::SA0);
  const CompactFault a_sa0 = stuck(xg, "a", FaultType::SA0);
  CHECK(classify_detection(xg.cg, good, ref.simulate_sequence(xg.cg, vec, &c_sa0, {})) ==
        XDetection::POSSIBLE);
  CHECK(classify_detection(xg.cg, good, ref.simulate_sequence(xg.cg, vec, &a_sa0, {})) ==
        XDetection::DEFINITE);

  FaultBatch batch;
  batch.size = 2;
  batch.faults[0] = c_sa0;
  batch.faults[1] = a_sa0;
  for (int i = 0; i < 2; ++i) {
    batch.faults[i].bit = static_cast<uint8_t>(i + 1);
    batch.faults[i].sa_mask = 1ULL << (i + 1);
    batch.mask |= batch.faults[i].sa_mask;
  }
  const XBatchDetection d = BitParallelSimX{}.detect_batch(xg.cg, vec, batch, {});
  CHECK(d.possible == (1ULL << 1));
  CHECK(d.definite == (1ULL << 2));
}

TEST_CASE("A possible fault is stored, credited, and upgraded by a definite detection",
          "[db][xmode]") {
  const std::string path = fresh_db("ff_xmode_status.db").string();
  const int64_t campaign = insert_campaign(path);
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path, campaign, false,
                                 false, false, "fail+x");
  const std::vector<int64_t> ids = all_fault_ids(path, campaign);
  REQUIRE(ids.size() >= 2);

  db::mark_faults_possible(path, {ids[0], ids[1]});
  CHECK(status_of(path, ids[0]) == "possible");
  CHECK(db::load_fault(path, ids[0]).status == FaultStatus::POSSIBLE);
  const db::CoverageSummary half = db::summarize(path, campaign, 0.5);
  CHECK(half.possible == 2);
  CHECK(half.detected == 0);
  CHECK_THAT(half.coverage_percent,
             Catch::Matchers::WithinAbs(100.0 * 1.0 / static_cast<double>(half.denominator),
                                        1e-9));
  CHECK(db::summarize(path, campaign, 0.0).coverage_percent == 0.0);

  // A definite detection upgrades a possible fault; a possible one never
  // downgrades a detected fault.
  const int64_t run = db::start_run(path, campaign, "test", 1);
  db::mark_faults_detected(path, campaign, run, 1, {ids[0]});
  CHECK(status_of(path, ids[0]) == "detected");
  db::mark_faults_possible(path, {ids[0]});
  CHECK(status_of(path, ids[0]) == "detected");
}

TEST_CASE("simulate_incremental_x marks definite detections and possible ones",
          "[xmode]") {
  const std::string path = fresh_db("ff_xmode_incremental.db").string();
  const int64_t campaign = insert_campaign(path);
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path, campaign, false,
                                 false, false, "fail+x");
  const CachedGraph& xg = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  const int64_t c_sa0 = fault_id(path, campaign, stuck(xg, "c", FaultType::SA0).net_index, "sa0");
  const int64_t a_sa0 = fault_id(path, campaign, stuck(xg, "a", FaultType::SA0).net_index, "sa0");
  const std::vector<int64_t> ids = all_fault_ids(path, campaign);
  const std::map<std::string, bool> vec{{"a", true}, {"c", true}};
  const int64_t run = db::start_run(path, campaign, "test", 2);

  const auto rows = atpg::simulate_incremental_x(fixture(), test::cell_map_path(), path,
                                                 campaign, run, {vec}, {"a", "c"}, ids, 1,
                                                 "fail+x");
  CHECK(status_of(path, a_sa0) == "detected");
  CHECK(status_of(path, c_sa0) == "possible");
  bool saw_c = false;
  for (const auto& row : rows) {
    if (row.fault_id == c_sa0) {
      saw_c = true;
      CHECK(row.possible);
    }
    if (row.fault_id == a_sa0) {
      CHECK_FALSE(row.possible);
    }
  }
  CHECK(saw_c);

  // Still a target, but reported possible only once.
  const auto again = atpg::simulate_incremental_x(fixture(), test::cell_map_path(), path,
                                                  campaign, run, {vec}, {"a", "c"}, ids, 2,
                                                  "fail+x");
  for (const auto& row : again) {
    CHECK(row.fault_id != c_sa0);
  }
  CHECK(status_of(path, c_sa0) == "possible");

  // Compaction's view of the same vector.
  const atpg::XDetections d = atpg::detect_with_vector_x(
      fixture(), test::cell_map_path(), vec, {"a", "c"},
      {{c_sa0, stuck(xg, "c", FaultType::SA0).net_index, FaultType::SA0},
       {a_sa0, stuck(xg, "a", FaultType::SA0).net_index, FaultType::SA0}},
      "fail+x");
  CHECK(d.definite == std::vector<int64_t>{a_sa0});
  CHECK(d.possible == std::vector<int64_t>{c_sa0});
}

TEST_CASE("SAT ATPG reads an x bit as an input the test cannot set", "[xmode][atpg]") {
  const std::string path = fresh_db("ff_xmode_sat.db").string();
  const int64_t campaign = insert_campaign(path);
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path, campaign, false,
                                 false, false, "fail+x");
  const CachedGraph& xg = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  const int64_t c_sa0 = fault_id(path, campaign, stuck(xg, "c", FaultType::SA0).net_index, "sa0");
  const int64_t a_sa0 = fault_id(path, campaign, stuck(xg, "a", FaultType::SA0).net_index, "sa0");

  // a stuck at 0: shown definitely only with c = 1 (with c = 0 the good y is
  // unknown). The dual-rail CNF must find exactly that.
  const atpg::SolveFaultResult a = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path, a_sa0, {}, 100000, 10, "fail+x");
  REQUIRE(a.result == "SAT");
  CHECK(a.vector.at("a"));
  CHECK(a.vector.at("c"));
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path, a_sa0, a.vector,
                                    "fail+x") == 2);

  // c stuck at 0 shows only when x = 0: no definite test, but one that the X
  // engine grades a possible detection.
  const atpg::SolveFaultResult c = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path, c_sa0, {}, 100000, 10, "fail+x");
  REQUIRE(c.result == "POSSIBLE");
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path, c_sa0, c.vector,
                                    "fail+x") == 1);

  // The two-valued graph ties x to 0: there c stuck at 0 is an ordinary SAT.
  const std::string two_path = fresh_db("ff_xmode_sat_two.db").string();
  const int64_t two_campaign = insert_campaign(two_path);
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), two_path, two_campaign,
                                 false, false, false, "fail");
  const CachedGraph& two = load_cached_graph(fixture(), test::cell_map_path(), "fail");
  const int64_t two_c = fault_id(two_path, two_campaign,
                                 stuck(two, "c", FaultType::SA0).net_index, "sa0");
  CHECK(atpg::solve_fault_for_db(fixture(), test::cell_map_path(), two_path, two_c, {},
                                 100000, 10, "fail")
            .result == "SAT");
}

TEST_CASE("A fault redundant whatever the unknowns stays redundant in X mode",
          "[xmode][atpg]") {
  // y = a & (c | x): every fault of it changes y for some a, c and value of x
  // (the x bit's own stuck-at-0 where x = 1, its stuck-at-1 where x = 0), so
  // none may be proven redundant -- definite or not.
  const std::string path = fresh_db("ff_xmode_redundant.db").string();
  const int64_t campaign = insert_campaign(path);
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path, campaign, false,
                                 false, false, "fail+x");
  for (const int64_t id : all_fault_ids(path, campaign)) {
    const atpg::SolveFaultResult r = atpg::solve_fault_for_db(
        fixture(), test::cell_map_path(), path, id, {}, 100000, 10, "fail+x");
    CHECK(r.result != "UNSAT");
  }
}
