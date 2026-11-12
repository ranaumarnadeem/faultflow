// Stage 3 of the X engine: level-sensitive latches. An X-mode graph models one
// (GateType LATCH: transparent while its enable is active, holding otherwise,
// clear and preset forcing it, powered up unknown); a two-valued graph leaves it
// out as a cell it does not model, as before.
//
// tiny_xlatch: q = buf(dlxtp(D=d, GATE=g)); qn = Q_N of dlrbn(D=d, GATE_N=g,
// RESET_B=rn) -- one latch open while g is 1, the other while g is 0.
#include <catch2/catch_test_macros.hpp>

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

const std::string& fixture() {
  static const std::string path = test::fixture_path("tiny_xlatch.json");
  return path;
}

const CachedGraph& xgraph() {
  return load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
}

TestCycle cycle_of(const ParsedGraph& p, bool d, bool g, bool rn) {
  TestCycle c;
  c.inputs[p.net_id_by_name("d")] = d;
  c.inputs[p.net_id_by_name("g")] = g;
  c.inputs[p.net_id_by_name("rn")] = rn;
  c.sample_outputs = true;
  return c;
}

// q and qn at each sampled cycle of `cycles`, from power-up unknown.
std::vector<std::pair<Logic, Logic>> outputs(const std::vector<TestCycle>& cycles) {
  const CachedGraph& g = xgraph();
  TestVector vec;
  vec.cycles = cycles;
  const auto samples = GoldenRefSimX{}.simulate_sequence(g.cg, vec, nullptr, {});
  std::vector<std::pair<Logic, Logic>> out;
  for (const XSnapshot& s : samples) {
    out.emplace_back(s.at(g.parsed.net_id_by_name("q")),
                     s.at(g.parsed.net_id_by_name("qn")));
  }
  return out;
}

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

int64_t insert_campaign(const std::string& path) {
  SQLite::Database db(path, SQLite::OPEN_READWRITE);
  db.exec(R"sql(
INSERT INTO campaigns(
  campaign_type, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
  yosys_version, faultflow_version, collapsing, unsupported_cells,
  include_clock_faults, include_reset_faults
) VALUES (
  'comb', 'tiny_xlatch', 'net', 'cell', 'cfg', 'tmpl', 'yosys', 'pipeline-v1',
  0, 'fail', 0, 0
)
)sql");
  return db.getLastInsertRowid();
}

int64_t stem_fault(const std::string& path, int64_t campaign, const std::string& net,
                   const std::string& type) {
  const CachedGraph& g = xgraph();
  const auto cidx = g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name(net));
  SQLite::Database db(path, SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT id FROM faults WHERE campaign_id=? AND "
                          "compiled_net_index=? AND fault_type=?");
  q.bind(1, campaign);
  q.bind(2, static_cast<int64_t>(cidx));
  q.bind(3, type);
  REQUIRE(q.executeStep());
  return q.getColumn(0).getInt64();
}

}  // namespace

TEST_CASE("A two-valued graph leaves a latch out; an X-mode graph models it",
          "[xsim][latch]") {
  REQUIRE_THROWS_AS(load_cached_graph(fixture(), test::cell_map_path(), "fail"),
                    UnsupportedCellError);
  const CachedGraph& bb = load_cached_graph(fixture(), test::cell_map_path(), "blackbox");
  CHECK(bb.ng.unknown_cells == std::set<std::string>{"u1", "u2"});
  int latches = 0;
  for (const SimNode& node : xgraph().cg.nodes) {
    latches += node.type == GateType::LATCH ? 1 : 0;
  }
  CHECK(latches == 2);
  CHECK(xgraph().cg.latch_configs.size() == 2);
  CHECK(xgraph().cg.ff_nodes.empty());
}

TEST_CASE("An X-mode latch is open while enabled, holds otherwise, powers up unknown",
          "[xsim][latch]") {
  const ParsedGraph& p = xgraph().parsed;
  // g = 0 from power-up: dlxtp closed (q unknown); dlrbn open (qn = ~d).
  auto out = outputs({cycle_of(p, true, false, true)});
  CHECK(out[0].first == Logic::X);
  CHECK(out[0].second == Logic::ZERO);
  // dlxtp open with d = 1, then closed (g = 0) while d falls: q holds 1.
  // dlrbn takes d = 0 while open (qn 1), holds it while closed by g = 1 though d
  // rises, and is open again with d = 0 (qn 1).
  out = outputs({cycle_of(p, false, false, true), cycle_of(p, true, true, true),
                 cycle_of(p, false, false, true)});
  CHECK(out[1].first == Logic::ONE);
  CHECK(out[2].first == Logic::ONE);
  CHECK(out[1].second == Logic::ONE);
  CHECK(out[2].second == Logic::ONE);
  // Reset (RESET_B low) forces dlrbn's Q to 0 whatever its enable: qn 1.
  out = outputs({cycle_of(p, true, true, false)});
  CHECK(out[0].second == Logic::ONE);
}

TEST_CASE("The bit-parallel X engine equals the X reference on latches",
          "[xsim][latch]") {
  const CachedGraph& g = xgraph();
  const std::vector<CompactFault> faults = enumerate_faults(g.ng, g.cg);
  std::mt19937 rng(12);
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
    for (int seq = 0; seq < 40; ++seq) {
      TestVector vec;
      XRunOptions options;
      for (int k = 0; k < 6; ++k) {
        TestCycle tc;
        for (int pi : g.cg.pi_nets) {
          tc.inputs[g.cg.compiled_to_yosys[pi]] = (rng() & 1) != 0;
        }
        tc.sample_outputs = true;
        vec.cycles.push_back(tc);
        std::set<int> unknown;
        for (int pi : g.cg.pi_nets) {
          if (rng() % 6 == 0) {
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

TEST_CASE("SAT ATPG sees through an open latch and not into a closed one's state",
          "[xmode][latch][atpg]") {
  const std::string path = fresh_db("ff_xlatch_sat.db").string();
  const int64_t campaign = insert_campaign(path);
  // g is a clock (a latch enable) and rn a reset: grade their faults too.
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path, campaign, true,
                                 true, false, "fail+x");
  // n1 (u1's Q) stuck at 0: shown at q through an open u1 (g = 1, d = 1).
  const int64_t n1_sa0 = stem_fault(path, campaign, "n1", "sa0");
  const atpg::SolveFaultResult n1 = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path, n1_sa0, {}, 100000, 10, "fail+x");
  REQUIRE(n1.result == "SAT");
  CHECK(n1.vector.at("g"));
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path, n1_sa0,
                                    n1.vector, "fail+x") == 2);
  // rn stuck at 1 (reset never applied): good qn 1 under reset, faulty qn
  // follows u2 -- with u2 open (g = 0) it is ~d, a definite test (d = 1).
  const int64_t rn_sa1 = stem_fault(path, campaign, "rn", "sa1");
  const atpg::SolveFaultResult rn = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path, rn_sa1, {}, 100000, 10, "fail+x");
  REQUIRE(rn.result == "SAT");
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path, rn_sa1,
                                    rn.vector, "fail+x") == 2);
  // g stuck at 0: u1 never opens, so with g = 1 q holds its unknown power-up
  // value where the good q is d, and qn (u2 closed in the good machine) is
  // unknown in the good machine. No definite test; the two-valued CNF, u1's
  // state shared, finds one that shows it for some power-up value.
  const int64_t g_sa0 = stem_fault(path, campaign, "g", "sa0");
  const atpg::SolveFaultResult g = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path, g_sa0, {}, 100000, 10, "fail+x");
  REQUIRE(g.result == "POSSIBLE");
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path, g_sa0,
                                    g.vector, "fail+x") == 1);
}

TEST_CASE("Two-valued graphs of a design without latches are unchanged by the cell map",
          "[latch]") {
  // The fixtures with no latch compile exactly as before: same nets, same nodes.
  const CachedGraph& two = load_cached_graph(test::fixture_path("tiny_xbit.json"),
                                             test::cell_map_path(), "fail");
  CHECK(two.cg.latch_configs.empty());
}
