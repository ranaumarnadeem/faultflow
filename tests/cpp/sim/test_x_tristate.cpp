// Stage 5 of the X engine: tristate buffers and buses. Each driver drives a
// private net (Z while disabled, no fault sites); a resolver drives the bus --
// one enabled driver its value, a conflict X, none X (a floating bus reads as X,
// as Z does at every cell input). A two-valued graph still refuses a TBUF.
//
// tiny_tbus: bus = ebufn(a, oe1_n) | ebufn(b, oe2_n); y = bus & c;
// z2 = einvp(c, e3) alone.
#include <catch2/catch_test_macros.hpp>

#include <SQLiteCpp/SQLiteCpp.h>

#include <filesystem>
#include <fstream>
#include <random>
#include <set>
#include <sstream>
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
  static const std::string path = test::fixture_path("tiny_tbus.json");
  return path;
}

using Inputs = std::vector<std::pair<std::string, bool>>;

std::vector<Logic> outputs(const std::string& path, const Inputs& in,
                           const std::vector<std::string>& outs) {
  const CachedGraph& g = load_cached_graph(path, test::cell_map_path(), "fail+x");
  TestVector vec;
  for (const auto& [pin, value] : in) {
    vec.inputs[g.parsed.net_id_by_name(pin)] = value;
  }
  const auto samples = GoldenRefSimX{}.simulate_sequence(g.cg, vec, nullptr, {});
  std::vector<Logic> row;
  for (const std::string& o : outs) {
    row.push_back(samples.at(0).at(g.parsed.net_id_by_name(o)));
  }
  return row;
}

Inputs bus_in(bool a, bool b, bool oe1_n, bool oe2_n) {
  return {{"a", a}, {"b", b}, {"oe1_n", oe1_n}, {"oe2_n", oe2_n}, {"c", true},
          {"e3", true}};
}

Logic lane(const Dual& d, int i) {
  const bool h = ((d.h >> i) & 1ULL) != 0;
  const bool l = ((d.l >> i) & 1ULL) != 0;
  if (h && l) {
    return Logic::X;
  }
  return h ? Logic::ONE : (l ? Logic::ZERO : Logic::Z);
}

// A netlist of `n` ebufn drivers d<i>/e<i> onto one output bus (temp file).
std::string wide_bus(int n) {
  std::ostringstream ports;
  std::ostringstream cells;
  std::ostringstream names;
  int bit = 2;
  const int bus = 1000;
  for (int i = 0; i < n; ++i) {
    const int d = bit++;
    const int e = bit++;
    ports << "\"d" << i << "\": {\"direction\": \"input\", \"bits\": [" << d << "]},"
          << "\"e" << i << "\": {\"direction\": \"input\", \"bits\": [" << e << "]},";
    cells << (i ? "," : "") << "\"u" << i
          << "\": {\"hide_name\": 0, \"type\": \"sky130_fd_sc_hd__ebufn_1\", "
             "\"parameters\": {}, \"attributes\": {}, \"port_directions\": "
             "{\"A\": \"input\", \"TE_B\": \"input\", \"Z\": \"output\"}, "
             "\"connections\": {\"A\": ["
          << d << "], \"TE_B\": [" << e << "], \"Z\": [" << bus << "]}}";
    names << "\"d" << i << "\": {\"hide_name\": 0, \"bits\": [" << d << "], \"attributes\": {}},"
          << "\"e" << i << "\": {\"hide_name\": 0, \"bits\": [" << e << "], \"attributes\": {}},";
  }
  std::ostringstream json;
  json << "{\"modules\": {\"wide\": {\"attributes\": {\"top\": \"1\"}, \"ports\": {"
       << ports.str() << "\"bus\": {\"direction\": \"output\", \"bits\": [" << bus
       << "]}}, \"cells\": {" << cells.str() << "}, \"netnames\": {" << names.str()
       << "\"bus\": {\"hide_name\": 0, \"bits\": [" << bus << "], \"attributes\": {}}}}}}";
  const auto path = std::filesystem::temp_directory_path() / "ff_wide_bus.json";
  std::ofstream(path) << json.str();
  return path.string();
}

}  // namespace

TEST_CASE("A two-valued graph refuses a tristate buffer", "[tristate]") {
  CHECK_THROWS_AS(load_cached_graph(fixture(), test::cell_map_path(), "fail"),
                  UnsupportedCellError);
  CHECK_THROWS_AS(load_cached_graph(fixture(), test::cell_map_path(), "blackbox"),
                  UnsupportedCellError);
}

TEST_CASE("An X-mode bus takes its enabled driver's value, X on a conflict or floating",
          "[xsim][tristate]") {
  const std::vector<std::string> outs{"bus", "y", "z2"};
  // One driver enabled (active low): its value.
  auto row = outputs(fixture(), bus_in(true, false, false, true), outs);
  CHECK(row[0] == Logic::ONE);
  CHECK(row[1] == Logic::ONE);
  CHECK(row[2] == Logic::ZERO);  // einvp: ~c while e3 is 1
  row = outputs(fixture(), bus_in(true, false, true, false), outs);
  CHECK(row[0] == Logic::ZERO);
  // Both enabled, agreeing: the value; disagreeing: X.
  CHECK(outputs(fixture(), bus_in(true, true, false, false), outs)[0] == Logic::ONE);
  CHECK(outputs(fixture(), bus_in(true, false, false, false), outs)[0] == Logic::X);
  // Neither: floating, read as X -- and y = X & c with c = 1.
  row = outputs(fixture(), bus_in(true, false, true, true), outs);
  CHECK(row[0] == Logic::X);
  CHECK(row[1] == Logic::X);
  // A lone driver disabled: its output floats too.
  CHECK(outputs(fixture(), {{"a", true}, {"b", true}, {"oe1_n", false}, {"oe2_n", true},
                            {"c", true}, {"e3", false}},
                outs)[2] == Logic::X);
}

TEST_CASE("A tristate driver's own net has no fault sites; the bus has them",
          "[xsim][tristate]") {
  const CachedGraph& g = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  std::set<uint32_t> sited;
  for (const CompactFault& f : enumerate_faults(g.ng, g.cg)) {
    sited.insert(f.net_index);
  }
  int internal = 0;
  for (const auto& [yid, net] : g.ng.nets) {
    if (net.is_internal) {
      ++internal;
      CHECK(sited.count(static_cast<uint32_t>(g.cg.yosys_to_compiled.at(yid))) == 0);
    }
  }
  CHECK(internal == 3);  // u1, u2, u4
  CHECK(sited.count(static_cast<uint32_t>(
            g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name("bus")))) == 1);
}

TEST_CASE("A bus of more than six drivers is merged six at a time", "[xsim][tristate]") {
  const std::string path = wide_bus(8);
  const CachedGraph& g = load_cached_graph(path, test::cell_map_path(), "fail+x");
  int merges = 0;
  for (const SimNode& node : g.cg.nodes) {
    merges += node.type == GateType::BUS_MERGE ? 1 : 0;
  }
  CHECK(merges == 1);
  Inputs in;
  for (int i = 0; i < 8; ++i) {
    in.emplace_back("d" + std::to_string(i), i == 7);
    in.emplace_back("e" + std::to_string(i), i != 7);  // only u7 enabled
  }
  CHECK(outputs(path, in, {"bus"})[0] == Logic::ONE);
  in.back().second = true;  // u7 disabled too: floating
  CHECK(outputs(path, in, {"bus"})[0] == Logic::X);
}

TEST_CASE("The bit-parallel X engine equals the X reference on tristate buses",
          "[xsim][tristate]") {
  std::mt19937 rng(77);
  for (const std::string& path : {fixture(), wide_bus(8)}) {
    const CachedGraph& g = load_cached_graph(path, test::cell_map_path(), "fail+x");
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
      for (int seq = 0; seq < 40; ++seq) {
        TestVector vec;
        XRunOptions options;
        for (int k = 0; k < 4; ++k) {
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
}

TEST_CASE("SAT ATPG drives a fault onto a bus through its one enabled driver",
          "[xmode][tristate][atpg]") {
  const auto path = std::filesystem::temp_directory_path() / "ff_xtbus_sat.db";
  std::filesystem::remove(path);
  db::init_database(path.string());
  int64_t campaign = 0;
  {
    SQLite::Database db(path.string(), SQLite::OPEN_READWRITE);
    db.exec(R"sql(
INSERT INTO campaigns(
  campaign_type, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
  yosys_version, faultflow_version, collapsing, unsupported_cells,
  include_clock_faults, include_reset_faults
) VALUES ('comb', 'tiny_tbus', 'n', 'c', 'k', 't', 'y', 'v', 0, 'fail', 0, 0)
)sql");
    campaign = db.getLastInsertRowid();
  }
  atpg::ensure_faults_enumerated(fixture(), test::cell_map_path(), path.string(), campaign,
                                 false, false, false, "fail+x");
  const CachedGraph& g = load_cached_graph(fixture(), test::cell_map_path(), "fail+x");
  const auto cidx = g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name("a"));
  SQLite::Database db(path.string(), SQLite::OPEN_READONLY);
  SQLite::Statement q(db, "SELECT id FROM faults WHERE campaign_id=? AND "
                          "compiled_net_index=? AND fault_type='sa0'");
  q.bind(1, campaign);
  q.bind(2, static_cast<int64_t>(cidx));
  REQUIRE(q.executeStep());
  const int64_t a_sa0 = q.getColumn(0).getInt64();
  const atpg::SolveFaultResult r = atpg::solve_fault_for_db(
      fixture(), test::cell_map_path(), path.string(), a_sa0, {}, 100000, 10, "fail+x");
  REQUIRE(r.result == "SAT");
  CHECK(r.vector.at("a"));
  CHECK_FALSE(r.vector.at("oe1_n"));  // u1 enabled
  CHECK(r.vector.at("oe2_n"));        // u2 not fighting it
  CHECK(atpg::verify_fault_vector_x(fixture(), test::cell_map_path(), path.string(), a_sa0,
                                    r.vector, "fail+x") == 2);
}
