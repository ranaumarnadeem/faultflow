#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <set>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"

using namespace faultflow;

namespace {

// Sky130 fixtures: use the default cell map (Sky130).
const char* kTinyFixtures[] = {
    "tiny_inv.json",
    "tiny_buf.json",
    "tiny_clkbuf.json",
    "tiny_and2.json",
    "tiny_or2.json",
    "tiny_nand2.json",
    "tiny_nand3.json",
    "tiny_nor2.json",
    "tiny_nor3.json",
    "tiny_xor2.json",
    "tiny_xnor2.json",
    "tiny_mux2.json",
    "tiny_aoi21.json",
    "tiny_aoi22.json",
    "tiny_oai21.json",
    "tiny_oai22.json",
    "tiny_chain.json",
    "tiny_reconverge.json",
    "tiny_const.json",
};

// OSU035-only fixtures (FAX1, HAX1): must use the OSU035 cell map explicitly.
const char* kOsu035Fixtures[] = {
    "tiny_addf.json",
    "tiny_addh.json",
};

}  // namespace

TEST_CASE("Tiny fixtures golden vs parallel", "[tiny_fixtures][bit_parallel]") {
  for (const char* fixture : kTinyFixtures) {
    INFO("fixture: " << fixture);
    const NormalizedGraph ng = test::load_normalized(fixture);
    const CompiledSimGraph cg = test::load_compiled(fixture);
    const auto faults = enumerate_faults(ng, cg);
    std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
    std::sort(pi_ids.begin(), pi_ids.end());
    const test::VectorSet vs = test::generate_complete_input_space(pi_ids);
    const auto mismatches =
        test::verify_parallel_matches_golden(cg, ng, faults, vs);
    REQUIRE(mismatches.empty());
  }
}

TEST_CASE("OSU035 tiny fixtures golden vs parallel", "[tiny_fixtures][bit_parallel][osu035]") {
  // FAX1 (ADDF) and HAX1 (ADDH) are OSU035-only cells; use the OSU035 cell map.
  const CellMap osu_map = CellMap::load(test::cell_map_path_osu());
  for (const char* fixture : kOsu035Fixtures) {
    INFO("fixture: " << fixture);
    const ParsedGraph pg = test::load_parsed(fixture);
    const NormalizedGraph ng = NormalizedGraph::from_parsed(pg, osu_map);
    const CompiledSimGraph cg = GraphCompiler::compile(ng);
    const auto faults = enumerate_faults(ng, cg);
    std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
    std::sort(pi_ids.begin(), pi_ids.end());
    const test::VectorSet vs = test::generate_complete_input_space(pi_ids);
    const auto mismatches =
        test::verify_parallel_matches_golden(cg, ng, faults, vs);
    REQUIRE(mismatches.empty());
  }
}

TEST_CASE("FaultEnumerator tiny_and2", "[tiny_fixtures][enumerator]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = enumerate_faults(ng, cg);
  REQUIRE(faults.size() == static_cast<size_t>(cg.net_count) * 2);
}

TEST_CASE("CompiledSimGraph ADDF lowering tiny_addf", "[tiny_fixtures][compiled]") {
  // FAX1 is an OSU035-only cell; use the OSU035 cell map explicitly.
  const ParsedGraph pg = test::load_parsed("tiny_addf.json");
  const CellMap osu_map = CellMap::load(test::cell_map_path_osu());
  const NormalizedGraph ng_addf = NormalizedGraph::from_parsed(pg, osu_map);
  const CompiledSimGraph cg = GraphCompiler::compile(ng_addf);
  int addf_s = 0;
  int addf_co = 0;
  const SimNode* s_node = nullptr;
  const SimNode* co_node = nullptr;
  for (const auto& sn : cg.nodes) {
    if (sn.type == GateType::ADDF_S) {
      ++addf_s;
      s_node = &sn;
    }
    if (sn.type == GateType::ADDF_CO) {
      ++addf_co;
      co_node = &sn;
    }
  }
  REQUIRE(addf_s == 1);
  REQUIRE(addf_co == 1);
  REQUIRE(s_node != nullptr);
  REQUIRE(co_node != nullptr);
  REQUIRE(s_node->out != co_node->out);
  REQUIRE(s_node->in0 != UNUSED_INPUT);
  REQUIRE(s_node->in1 != UNUSED_INPUT);
  REQUIRE(s_node->in2 != UNUSED_INPUT);
  REQUIRE(co_node->in0 != UNUSED_INPUT);
  REQUIRE(co_node->in1 != UNUSED_INPUT);
  REQUIRE(co_node->in2 != UNUSED_INPUT);
}

TEST_CASE("CompiledSimGraph fanout split tiny_reconverge", "[tiny_fixtures][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_reconverge.json");
  int buf_count = 0;
  for (const auto& sn : cg.nodes) {
    if (sn.type == GateType::BUF) {
      ++buf_count;
    }
  }
  REQUIRE(buf_count >= 1);
}

TEST_CASE("Canonical site keys tiny_reconverge fanout branches",
          "[tiny_fixtures][compiled][site_key]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_reconverge.json");
  std::set<std::string> keys;
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    keys.insert(canonical_site_key(cg, cidx));
  }
  REQUIRE(keys.count("net:2:stem") == 1);
  REQUIRE(keys.count("net:2:branch:u0:A") == 1);
  REQUIRE(keys.count("net:2:branch:u1:A") == 1);
  REQUIRE(keys.size() == static_cast<size_t>(cg.net_count));
}
