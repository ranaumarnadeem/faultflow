#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "scan/scan_pattern_sim.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::scan;

// The scan protocol fault grade samples only the nets it reads (the functional
// outputs, the scan-outs and, for LOC/LOS, the batch's fault nets). Every lane
// of every batch must match the scalar per-fault oracle, for every net of the
// fixture (scan-outs, flop Q stems, the lowest net ids) stuck at 0 and at 1,
// under stuck-at, LOC and LOS patterns.

namespace {

// A small deterministic bit source (the patterns only need to vary).
struct Bits {
  uint32_t state;
  bool next() {
    state = state * 1664525u + 1013904223u;
    return (state >> 16) & 1u;
  }
};

std::vector<ScanProtocolFaultSpec> every_fault(const std::string& fixture) {
  const CompiledSimGraph cg = test::load_compiled(fixture);
  std::vector<ScanProtocolFaultSpec> faults;
  // Three copies: more than one 63-lane batch, a fault in several lanes.
  for (int copy = 0; copy < 3; ++copy) {
    for (uint32_t net = 0; net < static_cast<uint32_t>(cg.net_count); ++net) {
      faults.push_back({net, 0});
      faults.push_back({net, 1});
    }
  }
  return faults;
}

enum class Mode { STUCK_AT, LOC, LOS };

ScanPatternRequest pattern(const std::string& fixture, Mode mode, Bits& bits) {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
  request.scan_enable_port = "scan_en";
  std::vector<std::string> pis;
  if (fixture == "tiny_scan_chain.json") {
    request.scan_input_ports = {"scan_in"};
    request.scan_output_ports = {"scan_out_0"};
    request.functional_output_ports = {"Q0", "Q1"};
    request.max_chain_length = 3;
    pis = {"D0", "D1", "D2"};
  } else {
    request.scan_input_ports = {"scan_in_0", "scan_in_1"};
    request.scan_output_ports = {"scan_out_0", "scan_out_1"};
    request.functional_output_ports = {"Q0", "Q2"};
    request.max_chain_length = 2;
    pis = {"D0", "D1", "D2", "D3"};
  }
  for (size_t chain = 0; chain < request.scan_input_ports.size(); ++chain) {
    std::vector<bool> load;
    for (int i = 0; i < request.max_chain_length; ++i) {
      load.push_back(bits.next());
    }
    request.load_seqs[static_cast<int>(chain)] = load;
    if (mode == Mode::LOS) {
      request.los_launch_scan_in[static_cast<int>(chain)] = bits.next();
    }
  }
  for (const std::string& pi : pis) {
    request.capture_pi_values[pi] = bits.next();
  }
  request.loc_two_capture = mode == Mode::LOC;
  request.los_two_capture = mode == Mode::LOS;
  return request;
}

void check_against_reference(const std::string& fixture, Mode mode) {
  Bits bits{mode == Mode::STUCK_AT ? 1u : mode == Mode::LOC ? 2u : 3u};
  const auto faults = every_fault(fixture);
  size_t detected = 0;
  for (int p = 0; p < 8; ++p) {
    ScanProtocolFaultRequest request;
    request.pattern = pattern(fixture, mode, bits);
    request.faults = faults;
    const auto graded = simulate_scan_protocol_faults(
        test::fixture_path(fixture), test::cell_map_path(), request, "fail");
    const auto reference = simulate_scan_protocol_faults_reference(
        test::fixture_path(fixture), test::cell_map_path(), request, "fail");
    REQUIRE(reference.size() == faults.size());
    size_t lanes = 0;
    for (const auto& batch : graded.batches) {
      for (const auto& lane : batch.lanes) {
        INFO("pattern " << p << " fault " << lane.fault_index << " net "
                        << faults[lane.fault_index].compiled_net_index << " sa"
                        << int(faults[lane.fault_index].fault_type));
        REQUIRE(lane.outcome == reference.at(lane.fault_index));
        detected += lane.outcome == ScanProtocolFaultOutcome::PASS;
        ++lanes;
      }
    }
    REQUIRE(lanes == faults.size());
  }
  REQUIRE(detected > 0);
}

}  // namespace

TEST_CASE("scan protocol grade == scalar oracle, every fault, stuck-at",
          "[scan][reference]") {
  check_against_reference("tiny_scan_chain.json", Mode::STUCK_AT);
  check_against_reference("tiny_scan_multichain.json", Mode::STUCK_AT);
}

TEST_CASE("scan protocol grade == scalar oracle, every fault, LOC",
          "[scan][reference][loc]") {
  check_against_reference("tiny_scan_chain.json", Mode::LOC);
  check_against_reference("tiny_scan_multichain.json", Mode::LOC);
}

TEST_CASE("scan protocol grade == scalar oracle, every fault, LOS",
          "[scan][reference][los]") {
  check_against_reference("tiny_scan_chain.json", Mode::LOS);
  check_against_reference("tiny_scan_multichain.json", Mode::LOS);
}

TEST_CASE("simulate_batch_samples on chosen nets == the full samples there",
          "[sim][scan]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_multichain.json");
  Bits bits{7u};
  TestVector vec;
  for (int c = 0; c < 9; ++c) {
    TestCycle cycle;
    for (int yid = 2; yid <= 9; ++yid) {  // CLK, scan ins, scan_en, D0..D3
      cycle.inputs[yid] = bits.next();
    }
    cycle.sample_outputs = c % 3 != 1;
    cycle.fault_active = c >= 2;
    vec.cycles.push_back(cycle);
  }
  FaultBatch batch;
  for (uint32_t net = 0; net < static_cast<uint32_t>(cg.net_count) &&
                         batch.size < kBatchSize;
       ++net) {
    CompactFault fault;
    fault.net_index = net;
    fault.type = net % 2 == 0 ? FaultType::SA0 : FaultType::SA1;
    fault.bit = static_cast<uint8_t>(batch.size + 1);
    fault.sa_mask = 1ULL << fault.bit;
    fault.exclusion = FaultExclusion::NONE;
    batch.faults[batch.size++] = fault;
    batch.mask |= fault.sa_mask;
  }
  const std::vector<uint32_t> nets = {
      static_cast<uint32_t>(cg.net_count - 1), 0u, 3u, 3u,
      static_cast<uint32_t>(cg.observable.front())};

  BitParallelSim sim;
  const auto full = sim.simulate_batch_samples(cg, vec, batch);
  const auto chosen = sim.simulate_batch_samples(cg, vec, batch, nets);
  REQUIRE(full.size() == 6);
  REQUIRE(chosen.size() == full.size());
  for (size_t t = 0; t < full.size(); ++t) {
    REQUIRE(chosen[t].size() == nets.size());
    for (size_t i = 0; i < nets.size(); ++i) {
      REQUIRE(chosen[t][i] == full[t][nets[i]]);
    }
  }
  REQUIRE_THROWS_AS(
      sim.simulate_batch_samples(cg, vec, batch,
                                 {static_cast<uint32_t>(cg.net_count)}),
      std::runtime_error);
}
