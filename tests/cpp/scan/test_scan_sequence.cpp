#include <catch2/catch_test_macros.hpp>

#include <stdexcept>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "scan/scan_pattern_sim.hpp"

using namespace faultflow;
using namespace faultflow::scan;

namespace {

ScanPatternRequest tiny_scan_chain_request() {
  ScanPatternRequest request;
  request.clock_port = "CLK";
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out_0"};
  request.functional_output_ports = {"Q0", "Q1"};
  request.max_chain_length = 3;
  request.load_seqs[0] = {true, false, true};
  request.capture_pi_values = {
      {"D0", true}, {"D1", false}, {"D2", true},
  };
  return request;
}

uint32_t compiled_index_for_yosys_net(const std::string& fixture,
                                      int yosys_net) {
  const CompiledSimGraph cg = test::load_compiled(fixture);
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    if (cg.compiled_to_yosys[cidx] == yosys_net) {
      return cidx;
    }
  }
  throw std::runtime_error("compiled index not found for yosys net");
}

}  // namespace

TEST_CASE("simulate_scan_pattern matches three-FF load/unload", "[scan]") {
  ScanPatternRequest request;
  request.clock_port = "CLK";
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out_0"};
  request.functional_output_ports = {"Q0", "Q1"};
  request.max_chain_length = 3;
  request.load_seqs[0] = {true, false, true};
  request.capture_pi_values = {
      {"D0", true}, {"D1", false}, {"D2", true},
  };

  const auto result = simulate_scan_pattern(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), request,
      "fail");

  REQUIRE(result.unload_seqs.at(0) == std::vector<bool>{true, false, true});
}

TEST_CASE("simulate_scan_pattern sample count is max_chain_length + 1", "[scan]") {
  ScanPatternRequest request;
  request.clock_port = "CLK";
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in_0", "scan_in_1"};
  request.scan_output_ports = {"scan_out_0", "scan_out_1"};
  request.functional_output_ports = {"Q0", "Q2"};
  request.max_chain_length = 2;
  request.load_seqs[0] = {true, false};
  request.load_seqs[1] = {false, true};
  request.capture_pi_values = {{"D0", true}, {"D1", false}, {"D2", false}, {"D3", true}};

  const auto result = simulate_scan_pattern(
      test::fixture_path("tiny_scan_multichain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(result.unload_seqs.at(0).size() == 2);
  REQUIRE(result.unload_seqs.at(1).size() == 2);
}

TEST_CASE("simulate_scan_protocol_faults golden matches simulate_scan_pattern",
          "[scan]") {
  const ScanPatternRequest pattern = tiny_scan_chain_request();
  ScanProtocolFaultRequest request;
  request.pattern = pattern;

  const auto direct = simulate_scan_pattern(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      pattern, "fail");
  const auto protocol_sim = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(scan_observations_equal(direct, protocol_sim.golden));
  REQUIRE(protocol_sim.batches.empty());
}

TEST_CASE("simulate_scan_protocol_faults detects capture fault on active D",
          "[scan]") {
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_request();
  const uint32_t d0 = compiled_index_for_yosys_net("tiny_scan_chain.json", 5);
  request.faults.push_back({d0, 0});  // SA0 on D0 while capture drives true

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(result.batches.size() == 1);
  REQUIRE(result.batches.front().lanes.size() == 1);
  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
}

TEST_CASE(
    "simulate_scan_protocol_faults inactive capture fault has no unload effect",
    "[scan]") {
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_request();
  request.pattern.capture_pi_values["D1"] = false;
  const uint32_t d1 = compiled_index_for_yosys_net("tiny_scan_chain.json", 6);
  request.faults.push_back({d1, 0});  // SA0 on D1 while capture drives false

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT);
}

TEST_CASE("simulate_scan_protocol_faults splits faults into ceil(n/63) batches",
          "[scan]") {
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_request();
  const uint32_t d2 = compiled_index_for_yosys_net("tiny_scan_chain.json", 7);
  request.faults.assign(64, {d2, 1});

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(result.batches.size() == 2);
  REQUIRE(result.batches[0].lanes.size() == 63);
  REQUIRE(result.batches[1].lanes.size() == 1);
}
