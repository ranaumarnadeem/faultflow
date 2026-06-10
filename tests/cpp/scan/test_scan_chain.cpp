#include <catch2/catch_test_macros.hpp>

#include "helpers/test_helpers.hpp"
#include "scan/scan_chain.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

TestCycle scan_cycle(bool clk, bool scan_in, bool scan_en, bool d0 = false,
                     bool d1 = false, bool d2 = false, bool sample = false) {
  TestCycle c;
  c.inputs = {{2, clk}, {3, scan_in}, {4, scan_en},
              {5, d0},  {6, d1},      {7, d2}};
  c.sample_outputs = sample;
  return c;
}

void append_shift_bit(TestVector& vec, bool bit, bool sample = false) {
  vec.cycles.push_back(scan_cycle(false, bit, true, false, false, false, false));
  vec.cycles.push_back(scan_cycle(true, bit, true, false, false, false, sample));
}

}  // namespace

TEST_CASE("Generic scan FF metadata normalizes and compiles", "[scan]") {
  const NormalizedGraph ng = test::load_normalized("tiny_scan_chain.json");
  const auto chains = scan::extract_scan_chains(ng);
  REQUIRE(chains.size() == 1);
  REQUIRE(chains.at(0).cells.size() == 3);
  REQUIRE(chains.at(0).scan_in_net == 3);
  REQUIRE(chains.at(0).scan_out_net == 10);

  const CompiledSimGraph cg = test::load_compiled("tiny_scan_chain.json");
  REQUIRE(cg.ff_configs.size() == 3);
  for (int node_index : cg.ff_nodes) {
    const SimNode& sn = cg.nodes.at(node_index);
    const CompiledFFConfig& cfg = cg.ff_configs.at(sn.ff_cfg);
    REQUIRE(cfg.has_scan);
    REQUIRE(sn.in4 != UNUSED_INPUT);
    REQUIRE(sn.in5 != UNUSED_INPUT);
  }
}

TEST_CASE("Scan shift moves SI through a three-FF chain", "[scan]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_chain.json");
  GoldenRefSim sim;

  TestVector vec;
  append_shift_bit(vec, true);
  append_shift_bit(vec, false);
  append_shift_bit(vec, true, true);

  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.size() == 1);
  REQUIRE(samples.back().at(8));
  REQUIRE_FALSE(samples.back().at(9));
  REQUIRE(samples.back().at(10));
}

TEST_CASE("Scan capture uses functional D when scan enable is low", "[scan]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_chain.json");
  GoldenRefSim sim;

  TestVector vec;
  vec.cycles = {
      scan_cycle(false, false, false, false, true, false, false),
      scan_cycle(true, false, false, false, true, false, true),
  };

  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.size() == 1);
  REQUIRE_FALSE(samples.back().at(8));
  REQUIRE(samples.back().at(9));
  REQUIRE_FALSE(samples.back().at(10));
}

TEST_CASE("Scan-out can be sampled before each shift-out edge", "[scan]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_chain.json");
  GoldenRefSim sim;

  TestVector vec;
  append_shift_bit(vec, true);
  append_shift_bit(vec, false);
  append_shift_bit(vec, true);
  vec.cycles.push_back(scan_cycle(false, false, true, false, false, false, true));
  vec.cycles.push_back(scan_cycle(true, false, true, false, false, false, false));
  vec.cycles.push_back(scan_cycle(false, false, true, false, false, false, true));
  vec.cycles.push_back(scan_cycle(true, false, true, false, false, false, false));
  vec.cycles.push_back(scan_cycle(false, false, true, false, false, false, true));

  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.size() == 3);
  REQUIRE(samples.at(0).at(10));
  REQUIRE_FALSE(samples.at(1).at(10));
  REQUIRE(samples.at(2).at(10));
}

TEST_CASE("Bit-parallel scan sequence matches GoldenRefSim detection",
          "[scan]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_scan_chain.json");
  CompactFault scan_in_sa0;
  scan_in_sa0.net_index = cg.yosys_to_compiled.at(3);
  scan_in_sa0.type = FaultType::SA0;

  TestVector vec;
  append_shift_bit(vec, true);
  append_shift_bit(vec, true);
  append_shift_bit(vec, true, true);

  GoldenRefSim golden;
  const auto ff = golden.simulate_sequence_fault_free(cg, vec);
  const auto faulty = golden.simulate_sequence_with_fault(cg, vec, scan_in_sa0);
  REQUIRE(golden.is_sequence_detected(cg, ff, faulty));

  BitParallelSim parallel;
  REQUIRE(parallel.simulate_single_fault(cg, vec, scan_in_sa0));
}
