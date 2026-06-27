#include <catch2/catch_test_macros.hpp>

#include <array>
#include <stdexcept>
#include <utility>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "scan/scan_pattern_sim.hpp"

using namespace faultflow;
using namespace faultflow::scan;

namespace {

ScanPatternRequest tiny_scan_chain_request() {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
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

ScanPatternRequest asymmetric_scan_chain_request() {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out"};
  request.functional_output_ports = {"Q0", "Q1"};
  request.max_chain_length = 3;
  request.load_seqs[0] = {false, false, true};
  request.capture_pi_values = {
      {"D0", true}, {"D1", false}, {"D2", false},
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
  request.clock_ports = {"CLK"};
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

TEST_CASE("asymmetric scan load reaches requested per-position state", "[scan]") {
  const auto result = simulate_scan_pattern(
      test::fixture_path("tiny_scan_order_asymmetric.json"),
      test::cell_map_path(), asymmetric_scan_chain_request(), "fail");

  REQUIRE(result.real_po_values.at("Q0"));
  REQUIRE_FALSE(result.real_po_values.at("Q1"));
  REQUIRE(result.unload_seqs.at(0) ==
          std::vector<bool>{false, false, true});
}

TEST_CASE("functional outputs are sampled before the capture edge", "[scan]") {
  ScanPatternRequest request = asymmetric_scan_chain_request();
  request.capture_pi_values = {
      {"D0", false}, {"D1", false}, {"D2", false},
  };

  const auto result = simulate_scan_pattern(
      test::fixture_path("tiny_scan_order_asymmetric.json"),
      test::cell_map_path(), request, "fail");

  REQUIRE(result.real_po_values.at("Q0"));
  REQUIRE_FALSE(result.real_po_values.at("Q1"));
  REQUIRE(result.unload_seqs.at(0) ==
          std::vector<bool>{false, false, false});
}

TEST_CASE("simulate_scan_pattern sample count is max_chain_length + 1", "[scan]") {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
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
    "parallel simulate_scan_protocol_faults is bit-identical to serial",
    "[scan][parallel]") {
  // Span several independent batch_idx blocks (130 faults -> 3 batches) by
  // cycling two real D-input nets with both stuck-at polarities, so parallelizing
  // across batches genuinely splits work. The per-batch lane outcomes must be
  // identical regardless of thread count.
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_request();
  const uint32_t d0 = compiled_index_for_yosys_net("tiny_scan_chain.json", 5);
  const uint32_t d1 = compiled_index_for_yosys_net("tiny_scan_chain.json", 6);
  const std::array<std::pair<uint32_t, uint8_t>, 4> sites = {
      {{d0, 0}, {d0, 1}, {d1, 0}, {d1, 1}}};
  for (int i = 0; i < 130; ++i) {
    request.faults.push_back({sites[i % sites.size()].first,
                              sites[i % sites.size()].second});
  }

  const auto run = [&](int sim_threads) {
    return simulate_scan_protocol_faults(
        test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
        request, "fail", sim_threads);
  };

  const ScanProtocolFaultSimResult serial = run(1);
  REQUIRE(serial.batches.size() == 3);  // ceil(130 / 63)

  for (const int threads : {2, 4, 8}) {
    const ScanProtocolFaultSimResult par = run(threads);
    REQUIRE(par.batches.size() == serial.batches.size());
    for (size_t b = 0; b < serial.batches.size(); ++b) {
      REQUIRE(par.batches[b].batch_index == serial.batches[b].batch_index);
      REQUIRE(par.batches[b].lanes.size() == serial.batches[b].lanes.size());
      for (size_t l = 0; l < serial.batches[b].lanes.size(); ++l) {
        REQUIRE(par.batches[b].lanes[l].fault_index ==
                serial.batches[b].lanes[l].fault_index);
        REQUIRE(par.batches[b].lanes[l].outcome ==
                serial.batches[b].lanes[l].outcome);
      }
    }
  }
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

TEST_CASE("scan protocol faults are inactive during shift-in", "[scan]") {
  ScanProtocolFaultRequest request;
  request.pattern = asymmetric_scan_chain_request();
  const uint32_t scan_in =
      compiled_index_for_yosys_net("tiny_scan_order_asymmetric.json", 3);
  request.faults.push_back({scan_in, 0});

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_order_asymmetric.json"),
      test::cell_map_path(), request, "fail");

  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT);
}

TEST_CASE("Q fault remains active and observable throughout unload", "[scan]") {
  ScanProtocolFaultRequest request;
  request.pattern = asymmetric_scan_chain_request();
  const uint32_t q2 =
      compiled_index_for_yosys_net("tiny_scan_order_asymmetric.json", 10);
  request.faults.push_back({q2, 1});

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_order_asymmetric.json"),
      test::cell_map_path(), request, "fail");

  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
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

// ---------------------------------------------------------------------------
// LOC two-capture protocol (Phase 4 Step 2b): shift V1 -> launch clock ->
// capture clock -> shift out. The launch pulse runs the fault-free machine to
// establish the launched state; the capture pulse activates the fault. The
// grading qualifier requires the good machine to make the required edge at the
// fault net between the launch (frame 0) and capture (frame 1) samples.
// ---------------------------------------------------------------------------

namespace {

ScanPatternRequest tiny_scan_chain_loc_request() {
  ScanPatternRequest request = tiny_scan_chain_request();
  request.loc_two_capture = true;
  return request;
}

}  // namespace

TEST_CASE("LOC two-capture preserves held-D unload", "[scan][loc]") {
  // tiny_scan_chain has D=PI, so the launch and capture clocks both load D: the
  // final state (hence the unload) matches the single-capture protocol. This
  // guards the sample indexing through the extra launch pulse.
  const auto result = simulate_scan_pattern(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      tiny_scan_chain_loc_request(), "fail");

  REQUIRE(result.unload_seqs.at(0) == std::vector<bool>{true, false, true});
}

TEST_CASE("LOC grades a launched FF-Q transition as detected", "[scan][loc]") {
  // ff0.Q scanned = load[2] = true (frame 0); the launch clock loads ff0.Q =
  // D0 = false (frame 1): a real 1->0 edge. STF (SA1) at ff0.Q is a transition,
  // observable at Q0.
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_loc_request();
  request.pattern.load_seqs[0] = {false, false, true};  // ff0 scanned = true
  request.pattern.capture_pi_values["D0"] = false;       // launched ff0.Q = false
  const uint32_t q0 = compiled_index_for_yosys_net("tiny_scan_chain.json", 8);
  request.faults.push_back({q0, 1});                     // STF at ff0.Q

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(),
      request, "fail");

  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
}

TEST_CASE("LOC rejects a stuck-at with no good-machine edge", "[scan][loc]") {
  // ff0.Q scanned = false (frame 0); launched ff0.Q = D0 = false (frame 1): NO
  // 1->0 edge. STF (SA1) still perturbs Q0 (stuck-1 vs good 0), so a plain
  // stuck-at WOULD be detected -- but it is not a transition, so LOC rejects it.
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_loc_request();
  request.pattern.load_seqs[0] = {false, false, false};  // ff0 scanned = false
  request.pattern.capture_pi_values["D0"] = false;        // launched ff0.Q = false
  const uint32_t q0 = compiled_index_for_yosys_net("tiny_scan_chain.json", 8);
  request.faults.push_back({q0, 1});                      // STF at ff0.Q

  const auto loc = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE(loc.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT);

  // The same fault WITHOUT the LOC qualifier (single-capture stuck-at) IS
  // detected -- proving the transition qualifier, not propagation, rejects it.
  ScanProtocolFaultRequest sa = request;
  sa.pattern.loc_two_capture = false;
  const auto single = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), sa,
      "fail");
  REQUIRE(single.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
}

// ---------------------------------------------------------------------------
// LOS two-capture protocol: launch = last scan shift (scan_enable asserted), so
// each FF transitions to its chain predecessor's loaded value, then a capture
// clock (scan_enable de-asserted) captures the response.
// ---------------------------------------------------------------------------

namespace {

ScanPatternRequest tiny_scan_chain_los_request() {
  ScanPatternRequest request = tiny_scan_chain_request();
  request.los_two_capture = true;
  return request;
}

}  // namespace

TEST_CASE("LOS grades a shift-launched FF-Q transition as detected",
          "[scan][los]") {
  // After loading, ff1.Q = load[1] = false (frame 0). The launch shift moves its
  // chain predecessor ff0's loaded value load[2] = true into ff1 (frame 1): a
  // real 0->1 edge at ff1.Q, observable at Q1.
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_los_request();
  request.pattern.load_seqs[0] = {false, false, true};  // ff1 frame0=0, frame1=load[2]=1
  request.pattern.los_launch_scan_in[0] = false;         // ff0's fresh launch bit
  const uint32_t q1 = compiled_index_for_yosys_net("tiny_scan_chain.json", 9);
  request.faults.push_back({q1, 0});                     // STR at ff1.Q

  const auto result = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE(result.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
}

TEST_CASE("LOS rejects a stuck-at with no shift-launched edge", "[scan][los]") {
  // ff1.Q = load[1] = false (frame 0); predecessor's load[2] = false too, so the
  // shift produces NO 1->0 edge. STF (SA1) still perturbs Q1, so single-capture
  // would detect it -- the LOS transition qualifier rejects it.
  ScanProtocolFaultRequest request;
  request.pattern = tiny_scan_chain_los_request();
  request.pattern.load_seqs[0] = {false, false, false};  // ff1 frame0=0, frame1=0
  request.pattern.los_launch_scan_in[0] = false;
  const uint32_t q1 = compiled_index_for_yosys_net("tiny_scan_chain.json", 9);
  request.faults.push_back({q1, 1});                     // STF at ff1.Q

  const auto los = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), request,
      "fail");
  REQUIRE(los.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT);

  ScanProtocolFaultRequest sa = request;
  sa.pattern.los_two_capture = false;
  const auto single = simulate_scan_protocol_faults(
      test::fixture_path("tiny_scan_chain.json"), test::cell_map_path(), sa,
      "fail");
  REQUIRE(single.batches.front().lanes.front().outcome ==
          ScanProtocolFaultOutcome::PASS);
}
