#include <catch2/catch_test_macros.hpp>

#include <stdexcept>
#include <string>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "scan/scan_pattern_sim.hpp"

using namespace faultflow;
using namespace faultflow::scan;

// tiny_scan_setup: sh (dfxtp on CLK) loads din when en, else holds; up (dfbbn,
// the falling edge of CLK, cleared by rst_n) loads sh when upd, else holds; ff0,
// the one scan flop, captures D0 & up. A setup program loads sh, then up.

namespace {

ScanPatternRequest capture_request() {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out"};
  request.functional_output_ports = {"mode_out"};
  request.max_chain_length = 1;
  request.load_seqs[0] = {false};
  request.capture_pi_values = {
      {"D0", true}, {"en", false}, {"upd", false}, {"rst_n", true}};
  return request;
}

std::vector<SetupCycle> load_then_update() {
  return {
      SetupCycle{{{"rst_n", false}}, {}},
      SetupCycle{{{"rst_n", true}, {"en", true}, {"din", true}}, {"CLK"}},
      // up takes sh on the falling edge of this cycle's pulse, upd still 1.
      SetupCycle{{{"rst_n", true}, {"upd", true}}, {"CLK"}},
  };
}

uint32_t compiled_index(const std::string& fixture, int yosys_net) {
  const CompiledSimGraph cg = test::load_compiled(fixture);
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    if (cg.compiled_to_yosys[cidx] == yosys_net) return cidx;
  }
  throw std::runtime_error("compiled index not found for yosys net");
}

}  // namespace

TEST_CASE("a setup program loads a falling-edge register before the pattern",
          "[scan][setup]") {
  ScanPatternRequest request = capture_request();
  const auto none = simulate_scan_pattern(
      test::fixture_path("tiny_scan_setup.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE_FALSE(none.real_po_values.at("mode_out"));
  REQUIRE(none.unload_seqs.at(0) == std::vector<bool>{false});

  request.setup_cycles = load_then_update();
  const auto set = simulate_scan_pattern(test::fixture_path("tiny_scan_setup.json"),
                                         test::cell_map_path(), request, "fail");
  REQUIRE(set.real_po_values.at("mode_out"));
  REQUIRE(set.unload_seqs.at(0) == std::vector<bool>{true});

  // Without its pulse's fall the update would see upd at 0 and miss.
  request.setup_cycles.back().pulse.clear();
  const auto unpulsed = simulate_scan_pattern(
      test::fixture_path("tiny_scan_setup.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE_FALSE(unpulsed.real_po_values.at("mode_out"));
}

TEST_CASE("a setup program runs fault-free", "[scan][setup]") {
  ScanProtocolFaultRequest request;
  request.pattern = capture_request();
  request.pattern.setup_cycles = load_then_update();
  // din stuck at 0 would load 0 into the mode bit if the program ran with it: it
  // doesn't, and din plays no part in the pattern itself.
  const uint32_t din = compiled_index("tiny_scan_setup.json", 6);
  // D0 stuck at 0 is caught at the capture.
  const uint32_t d0 = compiled_index("tiny_scan_setup.json", 9);
  request.faults = {{din, 0}, {d0, 0}};
  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_setup.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE(result.golden.unload_seqs.at(0) == std::vector<bool>{true});
  REQUIRE(result.batches.size() == 1);
  const auto& lanes = result.batches.front().lanes;
  REQUIRE(lanes.at(0).outcome == ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT);
  REQUIRE(lanes.at(1).outcome == ScanProtocolFaultOutcome::PASS);
}

TEST_CASE("a setup program can only pulse a scan clock", "[scan][setup]") {
  const auto error_of = [](const ScanPatternRequest& request) -> std::string {
    try {
      simulate_scan_pattern(test::fixture_path("tiny_scan_setup.json"),
                            test::cell_map_path(), request, "fail");
    } catch (const std::runtime_error& error) {
      return error.what();
    }
    return "";
  };
  ScanPatternRequest request = capture_request();
  request.setup_cycles = {SetupCycle{{{"en", true}}, {"din"}}};
  REQUIRE(error_of(request) == "test setup pulses din, which is not a scan clock");
  request.setup_cycles = {SetupCycle{{{"CLK", true}}, {}}};
  REQUIRE(error_of(request) ==
          "test setup sets scan clock CLK as a level; it can only pulse it");
}
