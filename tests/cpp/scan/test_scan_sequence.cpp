#include <catch2/catch_test_macros.hpp>

#include "helpers/test_helpers.hpp"
#include "scan/scan_pattern_sim.hpp"

using namespace faultflow;
using namespace faultflow::scan;

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
