// Stage 4 of the X engine: combinational loops. A two-valued graph refuses one,
// naming its cells (it used to simulate it silently wrong). An X-mode graph levels
// it by its strongly connected components and settles each loop by two-phase
// ternary simulation from its last values: a loop of gates that holds a value
// keeps it, one that does not settle reads X.
//
// tiny_loop_sr: q = nand(s_n, qb), qb = nand(r_n, q), y = q & a (q fans out).
// tiny_loop_ring: o = nand(en, o) through two inverters: holds 1 while en is 0,
// oscillates (X) while en is 1.
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_string.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "atpg/progressive_atpg.hpp"
#include "common/errors.hpp"
#include "db/sqlite_store.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/xsim/bit_parallel_sim_x.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

using namespace faultflow;

namespace {

const CachedGraph& xgraph(const std::string& name) {
  return load_cached_graph(test::fixture_path(name), test::cell_map_path(), "fail+x");
}

using Inputs = std::vector<std::pair<std::string, bool>>;

// `outs` at every cycle of `cycles`, from power-up unknown.
std::vector<std::vector<Logic>> run(const std::string& name,
                                    const std::vector<Inputs>& cycles,
                                    const std::vector<std::string>& outs) {
  const CachedGraph& g = xgraph(name);
  TestVector vec;
  for (const Inputs& in : cycles) {
    TestCycle c;
    for (const auto& [pin, value] : in) {
      c.inputs[g.parsed.net_id_by_name(pin)] = value;
    }
    c.sample_outputs = true;
    vec.cycles.push_back(c);
  }
  std::vector<std::vector<Logic>> rows;
  for (const XSnapshot& s : GoldenRefSimX{}.simulate_sequence(g.cg, vec, nullptr, {})) {
    std::vector<Logic> row;
    for (const std::string& o : outs) {
      row.push_back(s.at(g.parsed.net_id_by_name(o)));
    }
    rows.push_back(row);
  }
  return rows;
}

Inputs sr(bool s_n, bool r_n) { return {{"s_n", s_n}, {"r_n", r_n}, {"a", true}}; }

Logic lane(const Dual& d, int i) {
  const bool h = ((d.h >> i) & 1ULL) != 0;
  const bool l = ((d.l >> i) & 1ULL) != 0;
  if (h && l) {
    return Logic::X;
  }
  return h ? Logic::ONE : (l ? Logic::ZERO : Logic::Z);
}

std::filesystem::path fresh_db(const std::string& name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  std::filesystem::remove(path);
  db::init_database(path.string());
  return path;
}

int64_t insert_campaign(const std::string& path, const std::string& top) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  SQLite::Statement q(db, R"sql(
INSERT INTO campaigns(
  campaign_type, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
  yosys_version, faultflow_version, collapsing, unsupported_cells,
  include_clock_faults, include_reset_faults
) VALUES ('comb', ?, 'net', 'cell', 'cfg', 'tmpl', 'yosys', 'pipeline-v1', 0, 'fail', 0, 0)
)sql");
  q.bind(1, top);
  q.exec();
  return db.getLastInsertRowid();
}

}  // namespace

TEST_CASE("A two-valued graph refuses a combinational loop and names its cells",
          "[loop]") {
  for (const auto& [name, cells] :
       std::vector<std::pair<std::string, std::vector<std::string>>>{
           {"tiny_loop_sr.json", {"u1", "u2"}},
           {"tiny_loop_ring.json", {"u1", "u2", "u3"}}}) {
    INFO(name);
    try {
      load_cached_graph(test::fixture_path(name), test::cell_map_path(), "fail");
      FAIL("a cyclic netlist compiled two-valued");
    } catch (const CombinationalLoopError& e) {
      const std::string what = e.what();
      for (const std::string& cell : cells) {
        CHECK_THAT(what, Catch::Matchers::ContainsSubstring(cell));
      }
      CHECK_THAT(what, Catch::Matchers::ContainsSubstring("engine = x"));
    }
  }
  // An acyclic netlist is unaffected.
  CHECK_NOTHROW(load_cached_graph(test::fixture_path("tiny_xbit.json"),
                                  test::cell_map_path(), "fail"));
}

TEST_CASE("A loop of gates that holds a value keeps it across passes", "[xsim][loop]") {
  const auto rows = run("tiny_loop_sr.json",
                        {sr(true, true),     // power-up: unknown, held
                         sr(false, true),    // set: q 1
                         sr(true, true),     // hold 1
                         sr(true, false),    // reset: q 0
                         sr(true, true),     // hold 0
                         sr(false, false),   // both: q and qb 1
                         sr(true, true)},    // both released at once: a race, X
                        {"q", "qb", "y"});
  CHECK(rows[0][0] == Logic::X);
  CHECK(rows[1][0] == Logic::ONE);
  CHECK(rows[1][1] == Logic::ZERO);
  CHECK(rows[2][0] == Logic::ONE);
  CHECK(rows[2][1] == Logic::ZERO);
  CHECK(rows[2][2] == Logic::ONE);  // y = q & a, q's other branch
  CHECK(rows[3][0] == Logic::ZERO);
  CHECK(rows[3][1] == Logic::ONE);
  CHECK(rows[4][0] == Logic::ZERO);
  CHECK(rows[4][1] == Logic::ONE);
  CHECK(rows[5][0] == Logic::ONE);
  CHECK(rows[5][1] == Logic::ONE);
  CHECK(rows[6][0] == Logic::X);
  CHECK(rows[6][1] == Logic::X);
}

TEST_CASE("A loop that does not settle reads X", "[xsim][loop]") {
  const auto rows = run("tiny_loop_ring.json",
                        {{{"en", false}}, {{"en", true}}, {{"en", false}}}, {"o"});
  CHECK(rows[0][0] == Logic::ONE);
  CHECK(rows[1][0] == Logic::X);
  CHECK(rows[2][0] == Logic::ONE);
}

TEST_CASE("The bit-parallel X engine equals the X reference on loops", "[xsim][loop]") {
  std::mt19937 rng(44);
  for (const std::string name : {"tiny_loop_sr.json", "tiny_loop_ring.json"}) {
    INFO(name);
    const CachedGraph& g = xgraph(name);
    const std::vector<CompactFault> faults = enumerate_faults(g.ng, g.cg);
    for (size_t i = 0; i < faults.size(); i += kBatchSize) {
      FaultBatch batch;
      for (size_t j = i; j < std::min(faults.size(), i + kBatchSize); ++j) {
        CompactFault f = faults[j];
        f.exclusion = FaultExclusion::NONE;
        f.bit = static_cast<uint8_t>(batch.size + 1);
        f.sa_mask = 1ULL << f.bit;
        batch.faults[batch.size++] = f;
        batch.mask |= f.sa_mask;
      }
      for (int seq = 0; seq < 60; ++seq) {
        TestVector vec;
        XRunOptions options;
        options.initial_state = (seq % 3 == 0) ? Logic::ZERO : Logic::X;
        for (int k = 0; k < 7; ++k) {
          TestCycle tc;
          for (int pi : g.cg.pi_nets) {
            tc.inputs[g.cg.compiled_to_yosys[pi]] = (rng() & 1) != 0;
          }
          tc.sample_outputs = (rng() % 3) != 0;
          tc.fault_active = (rng() % 4) != 0;
          vec.cycles.push_back(tc);
          std::set<int> unknown;
          for (int pi : g.cg.pi_nets) {
            if (rng() % 7 == 0) {
              unknown.insert(g.cg.compiled_to_yosys[pi]);
            }
          }
          options.unknown_inputs.push_back(unknown);
        }
        const auto got = BitParallelSimX{}.simulate_batch_samples(g.cg, vec, batch, options);
        for (int b = 0; b <= batch.size; ++b) {
          const CompactFault* fault = b == 0 ? nullptr : &batch.faults[b - 1];
          const auto want = GoldenRefSimX{}.simulate_sequence(g.cg, vec, fault, options);
          REQUIRE(want.size() == got.size());
          for (size_t s = 0; s < want.size(); ++s) {
            for (const auto& [yid, cidx] : g.cg.yosys_to_compiled) {
              REQUIRE(lane(got[s][static_cast<size_t>(cidx)], b) == want[s].at(yid));
            }
          }
        }
      }
    }
  }
}

TEST_CASE("SAT ATPG never proves a fault redundant through a loop", "[xmode][loop][atpg]") {
  const std::string fixture = test::fixture_path("tiny_loop_sr.json");
  const std::string path = fresh_db("ff_xloop_sat.db").string();
  const int64_t campaign = insert_campaign(path, "tiny_loop_sr");
  atpg::ensure_faults_enumerated(fixture, test::cell_map_path(), path, campaign, false,
                                 false, false, "fail+x");
  SQLite::Database db(path, SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT id FROM faults WHERE campaign_id=? AND exclusion='none'");
  q.bind(1, campaign);
  int solved = 0;
  while (q.executeStep()) {
    const int64_t id = q.getColumn(0).getInt64();
    const atpg::SolveFaultResult r = atpg::solve_fault_for_db(
        fixture, test::cell_map_path(), path, id, {}, 100000, 10, "fail+x");
    INFO(id);
    CHECK(r.result != "UNSAT");
    if (r.result == "SAT") {
      // A candidate only: the X engine, which settles the loop, grades it.
      CHECK(atpg::verify_fault_vector_x(fixture, test::cell_map_path(), path, id,
                                        r.vector, "fail+x") >= 0);
    }
    ++solved;
  }
  CHECK(solved > 0);
}
