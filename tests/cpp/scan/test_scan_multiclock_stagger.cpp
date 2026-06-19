// Tests for staggered multi-clock launch/capture (Phase 6b, step 1).
//
// Fixture: tests/fixtures/multi_clock/two_domain_3ff.json
//   Domain A (clk_a): ff0_a (D=d0, Q=net11), ff1_a (D=xor_out=q0_a^d1, Q=net13)
//   Domain B (clk_b): ff0_b (D=and_out=q1_a&d2, Q=net15)
//   Scan chain A: scan_in_a -> ff0_a -> ff1_a -> scan_out_a (length 2)
//   Scan chain B: scan_in_b -> ff0_b -> scan_out_b (length 1)
//   max_chain_length = 2 (longest chain)

#include <catch2/catch_test_macros.hpp>

#include <string>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "scan/scan_pattern_sim.hpp"

using namespace faultflow;
using namespace faultflow::scan;

namespace {

// Absolute path to the multi-clock post-stitch fixture.
std::string multiclock_fixture() {
  return std::string(FAULTFLOW_SOURCE_DIR) +
         "/tests/fixtures/multi_clock/two_domain_3ff.json";
}

// Base request: load ff0_b=1, domain-A FFs loaded to 0.
// capture_pi_values: all false (d0=0, d1=0, d2=0).
// In full-clock LOC: ff0_b would capture and_out = q1_a AND d2 = 0 AND 0 = 0.
//
// Chain B has length 1 but max_chain_length=2, so two scan pulses fire.
// The LAST pulse is what ff0_b retains (it captures scan_in_b at offset=1).
// load_seqs[1] = {false/*pad*/, true/*ff0_b*/} ensures ff0_b.Q=true after load.
ScanPatternRequest base_request_b_loaded_1() {
  ScanPatternRequest req;
  req.clock_ports = {"clk_a", "clk_b"};
  req.scan_enable_port = "scan_en";
  req.scan_input_ports = {"scan_in_a", "scan_in_b"};
  req.scan_output_ports = {"scan_out_a", "scan_out_b"};
  req.functional_output_ports = {"Q0_A"};
  req.max_chain_length = 2;
  // Chain A: [bit_for_ff1_a, bit_for_ff0_a] = [false, false]
  req.load_seqs[0] = {false, false};
  // Chain B (length 1): [pad, bit_for_ff0_b] = [false, true]
  req.load_seqs[1] = {false, true};
  req.capture_pi_values = {{"d0", false}, {"d1", false}, {"d2", false}};
  return req;
}

// Base request: load ff0_a=1 (chain A second element), domain-B FF loaded to 0.
// In full-clock LOC: ff0_a would capture d0=false -> ff0_a.Q=0.
ScanPatternRequest base_request_a0_loaded_1() {
  ScanPatternRequest req;
  req.clock_ports = {"clk_a", "clk_b"};
  req.scan_enable_port = "scan_en";
  req.scan_input_ports = {"scan_in_a", "scan_in_b"};
  req.scan_output_ports = {"scan_out_a", "scan_out_b"};
  req.functional_output_ports = {"Q0_A"};
  req.max_chain_length = 2;
  // Chain A: [ff1_a_bit, ff0_a_bit] = [false, true] -> ff0_a=1, ff1_a=0
  req.load_seqs[0] = {false, true};
  // Chain B: [ff0_b_bit] = [false]
  req.load_seqs[1] = {false};
  req.capture_pi_values = {{"d0", false}, {"d1", false}, {"d2", false}};
  return req;
}

}  // namespace

// ---------------------------------------------------------------------------
// 6B-C01: empty active_clock_ports -> byte-identical to default
// ---------------------------------------------------------------------------
TEST_CASE("stagger: empty active_clock_ports is byte-identical to default",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest req = base_request_b_loaded_1();
  // Explicitly set empty — same as default.
  req.active_clock_ports = {};
  req.loc_two_capture = true;

  const auto r1 = simulate_scan_pattern(multiclock_fixture(),
                                        test::cell_map_path(), req, "fail");

  ScanPatternRequest req2 = req;
  req2.active_clock_ports.clear();  // same as {}
  const auto r2 = simulate_scan_pattern(multiclock_fixture(),
                                        test::cell_map_path(), req2, "fail");

  REQUIRE(scan_observations_equal(r1, r2));
}

// ---------------------------------------------------------------------------
// 6B-C02: active_clock_ports={"clk_a"} -> domain-B FF holds loaded value
// ---------------------------------------------------------------------------
TEST_CASE("stagger: LOC clk_a only - domain-B FF holds its loaded value",
          "[scan][multiclock_stagger]") {
  // Load ff0_b = 1, run LOC with only clk_a active.
  // and_out = q1_a AND d2 = 0 AND 0 = 0.
  // If clk_b fires: ff0_b captures 0 -> unload_seqs[1][0] = 0.
  // If clk_b held:  ff0_b stays 1  -> unload_seqs[1][0] = 1. (correct)
  ScanPatternRequest req = base_request_b_loaded_1();
  req.loc_two_capture = true;
  req.active_clock_ports = {"clk_a"};

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(1));
  // ff0_b should have held its loaded value (1) since clk_b was inactive.
  REQUIRE(r.unload_seqs.at(1).at(0) == true);
}

// Contrast: with both clocks active, ff0_b captures and_out=0.
TEST_CASE("stagger: LOC both clocks active - domain-B FF captures (contrast)",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest req = base_request_b_loaded_1();
  req.loc_two_capture = true;
  // active_clock_ports empty = all clocks

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(1));
  // ff0_b clocked, captures and_out = q1_a AND d2 = 0 AND 0 = 0.
  REQUIRE(r.unload_seqs.at(1).at(0) == false);
}

// ---------------------------------------------------------------------------
// 6B-C03: active_clock_ports={"clk_b"} -> domain-A FFs hold loaded values
// ---------------------------------------------------------------------------
TEST_CASE("stagger: LOC clk_b only - domain-A FFs hold their loaded values",
          "[scan][multiclock_stagger]") {
  // Load ff0_a=1, ff1_a=0. Run LOC with only clk_b active.
  // d0=false: if clk_a fired, ff0_a would capture d0=false -> ff0_a.Q=0.
  // Unload chain A: [ff1_a.Q, ff0_a.Q] = [0, 1] if no capture occurred.
  ScanPatternRequest req = base_request_a0_loaded_1();
  req.loc_two_capture = true;
  req.active_clock_ports = {"clk_b"};

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(0));
  const auto& chain_a = r.unload_seqs.at(0);
  REQUIRE(chain_a.size() == 2);
  // ff1_a holds 0 (loaded), ff0_a holds 1 (loaded). Unload: [ff1_a, ff0_a] = [0, 1].
  REQUIRE(chain_a.at(0) == false);  // ff1_a held
  REQUIRE(chain_a.at(1) == true);   // ff0_a held
}

// Contrast: with both clocks active, ff0_a captures d0=false -> ff0_a.Q=0.
TEST_CASE("stagger: LOC both clocks active - domain-A FF captures (contrast)",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest req = base_request_a0_loaded_1();
  req.loc_two_capture = true;

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(0));
  const auto& chain_a = r.unload_seqs.at(0);
  // ff0_a captured d0=false, ff1_a captured xor_out = q0_a_after_launch XOR d1.
  // After launch: ff0_a.Q = false (captured d0=false).
  // After capture: ff1_a.Q = false XOR false = false.
  // Unload: [ff1_a=false, ff0_a=false].
  REQUIRE(chain_a.at(0) == false);
  REQUIRE(chain_a.at(1) == false);
}

// ---------------------------------------------------------------------------
// 6B-C06: LOS active_clock_ports={"clk_a"} -> domain-B FF holds
// ---------------------------------------------------------------------------
TEST_CASE("stagger: LOS clk_a only - domain-B FF holds its loaded value",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest req = base_request_b_loaded_1();
  req.los_two_capture = true;
  req.active_clock_ports = {"clk_a"};
  // LOS launch scan-in: don't set (defaults to false for all chains).

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(1));
  // Domain-B FF should hold its loaded value (1) since clk_b was inactive.
  REQUIRE(r.unload_seqs.at(1).at(0) == true);
}

// ---------------------------------------------------------------------------
// 6B-C07: LOS active_clock_ports={"clk_b"} -> domain-A FFs hold
// ---------------------------------------------------------------------------
TEST_CASE("stagger: LOS clk_b only - domain-A FFs hold their loaded values",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest req = base_request_a0_loaded_1();
  req.los_two_capture = true;
  req.active_clock_ports = {"clk_b"};

  const auto r = simulate_scan_pattern(multiclock_fixture(),
                                       test::cell_map_path(), req, "fail");

  REQUIRE(r.unload_seqs.count(0));
  const auto& chain_a = r.unload_seqs.at(0);
  REQUIRE(chain_a.size() == 2);
  // Domain-A FFs hold: ff1_a=0 (loaded), ff0_a=1 (loaded).
  REQUIRE(chain_a.at(0) == false);
  REQUIRE(chain_a.at(1) == true);
}

// ---------------------------------------------------------------------------
// 6B-C04/C05: LOC fault sim with staggered clocking runs and returns batches
// ---------------------------------------------------------------------------
TEST_CASE("stagger: LOC fault sim clk_a active returns batch result",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest pattern = base_request_b_loaded_1();
  pattern.loc_two_capture = true;
  pattern.active_clock_ports = {"clk_a"};

  ScanProtocolFaultRequest req;
  req.pattern = pattern;
  // Inject SA0 on compiled net index 0 as a smoke test.
  ScanProtocolFaultSpec spec;
  spec.compiled_net_index = 0;
  spec.fault_type = 0;
  req.faults.push_back(spec);

  const auto result = simulate_scan_protocol_faults(
      multiclock_fixture(), test::cell_map_path(), req, "fail");

  REQUIRE(!result.batches.empty());
  REQUIRE(result.batches[0].lanes.size() == 1);
}

TEST_CASE("stagger: LOC fault sim clk_b active returns batch result",
          "[scan][multiclock_stagger]") {
  ScanPatternRequest pattern = base_request_a0_loaded_1();
  pattern.loc_two_capture = true;
  pattern.active_clock_ports = {"clk_b"};

  ScanProtocolFaultRequest req;
  req.pattern = pattern;
  ScanProtocolFaultSpec spec;
  spec.compiled_net_index = 0;
  spec.fault_type = 1;
  req.faults.push_back(spec);

  const auto result = simulate_scan_protocol_faults(
      multiclock_fixture(), test::cell_map_path(), req, "fail");

  REQUIRE(!result.batches.empty());
  REQUIRE(result.batches[0].lanes.size() == 1);
}
