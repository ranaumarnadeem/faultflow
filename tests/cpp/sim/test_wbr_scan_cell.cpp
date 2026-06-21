#include <catch2/catch_test_macros.hpp>

#include <map>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "scan/scan_pattern_sim.hpp"

// Stage 4 — native shiftable IEEE 1500 WBR cell ($wbc_*_scan_faultflow).
// The cell lowers into TWO nodes: a plain scan FF (drives the scan-chain wire q
// = CTO) and a mode-dependent output mux (drives the functional output net from
// CFI in FUNCTIONAL, from q in INTEST/EXTEST). These tests prove the lowering +
// the mode-aware sequential scan-protocol sim: a value loaded into the wrapper
// chain drives the core in INTEST, and the core's response is captured + unloaded.
//
// Fixture tiny_wrapped_core_scan.json: chain wsi -> wi0 -> wi1 -> wo0 -> wso.
//   wi0/wi1 ($wbc_in_scan): q drives core inputs ci0/ci1 in INTEST.
//   g_core: co = ci0 ^ ci1.
//   wo0 ($wbc_out_scan): captures co; unloaded at wso.

using namespace faultflow;
using faultflow::scan::ScanPatternRequest;
using faultflow::scan::simulate_scan_pattern;

namespace {

ScanPatternRequest base_request(TestMode mode, bool x0, bool x1) {
  ScanPatternRequest req;
  req.clock_ports = {"CLK"};
  req.clock_off_states = {false};
  req.scan_enable_port = "SE";
  req.scan_input_ports = {"wsi"};
  req.scan_output_ports = {"wso"};
  req.max_chain_length = 3;
  // Descending-position load: offset 0 ends at the chain TAIL (wo0, pos 2),
  // offset 2 ends at the HEAD (wi0, pos 0). So {wo0, wi1, wi0} = {x2, x1, x0}.
  req.load_seqs[0] = {false, x1, x0};  // wo0=dont-care, wi1=x1, wi0=x0
  req.capture_pi_values = {{"sysA", false}, {"sysB", false}};
  req.test_mode = mode;
  return req;
}

}  // namespace

TEST_CASE("scan WBR cell lowers into FF + mode-mux (two nodes per cell)",
          "[wbr][scan][lowering]") {
  ParsedGraph pg = test::load_parsed("tiny_wrapped_core_scan.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map);
  CompiledSimGraph cg = GraphCompiler::compile(ng);

  // Three wrapper cells, all flagged scan, each with a valid CTO (q) index.
  REQUIRE(cg.wrapper_cells.size() == 3);
  for (const auto& wc : cg.wrapper_cells) {
    REQUIRE(wc.scan);
    REQUIRE(wc.cto_idx != UNUSED_INPUT);
  }
  // The scan-FF halves are real FFs (state-holding, schedulable as scan cells).
  REQUIRE(cg.ff_nodes.size() == 3);
}

TEST_CASE("scan WBR INTEST: loaded q drives the core; response unloads",
          "[wbr][scan][intest]") {
  const std::string fixture = test::fixture_path("tiny_wrapped_core_scan.json");
  const std::string cm = test::cell_map_path();

  // co = x0 ^ x1, delivered to the core from the loaded boundary register.
  for (int mask = 0; mask < 4; ++mask) {
    const bool x0 = (mask & 1) != 0;
    const bool x1 = (mask & 2) != 0;
    const auto req = base_request(TestMode::INTEST, x0, x1);
    const auto res = simulate_scan_pattern(fixture, cm, req, "blackbox");
    REQUIRE(res.unload_seqs.count(0) == 1);
    REQUIRE(res.unload_seqs.at(0).size() == 3);
    // First unloaded bit (descending position) is wo0.q = captured core output.
    REQUIRE(res.unload_seqs.at(0).at(0) == (x0 ^ x1));
  }
}

TEST_CASE("scan WBR FUNCTIONAL differs from INTEST (mode-mux is real)",
          "[wbr][scan][functional]") {
  const std::string fixture = test::fixture_path("tiny_wrapped_core_scan.json");
  const std::string cm = test::cell_map_path();

  // x0=1, x1=0 -> INTEST drives the core from q -> co = 1.
  const auto intest = simulate_scan_pattern(
      fixture, cm, base_request(TestMode::INTEST, true, false), "blackbox");
  REQUIRE(intest.unload_seqs.at(0).at(0) == true);

  // FUNCTIONAL: the mode-mux passes CFI (sysA/sysB = 0), NOT the loaded q, so
  // the core sees 0 ^ 0 = 0 regardless of what was shifted in.
  const auto functional = simulate_scan_pattern(
      fixture, cm, base_request(TestMode::FUNCTIONAL, true, false), "blackbox");
  REQUIRE(functional.unload_seqs.at(0).at(0) == false);
}
