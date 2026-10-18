#include <catch2/catch_test_macros.hpp>

#include <vector>

#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

// tiny_dff_fanout.json: CLK 2; D 3 -> inv (8) -> u0 (Q0 4) and u1 (Q1 5); E 6 -> inv
// (9) -> u2 (Q2 7). D's flops read their D through fanout branches, E's flop its own.
constexpr int kClk = 2, kD = 3, kE = 6, kQ0 = 4, kQ1 = 5, kQ2 = 7;

TestCycle at(bool clk, bool d) {
  TestCycle c;
  c.inputs = {{kClk, clk}, {kD, d}, {kE, d}};
  c.sample_outputs = true;
  return c;
}

}  // namespace

TEST_CASE("A flop samples what its D held before the cycle that clocks it",
          "[sequential][edge]") {
  // Cycle 1 changes the inputs and raises the clock together: every flop takes
  // the D settled before it (the inputs at 0: D inverted, 1), whatever its D's
  // fanout. Cycle 3's edge, the inputs unchanged since cycle 2, takes theirs (0).
  const CompiledSimGraph cg = test::load_compiled("tiny_dff_fanout.json");
  TestVector vec;
  vec.cycles = {at(false, false), at(true, true), at(false, true), at(true, true)};
  GoldenRefSim golden;
  const auto samples = golden.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.size() == 4);
  for (const int q : {kQ0, kQ1, kQ2}) {
    INFO("Q net " << q);
    REQUIRE(samples[1].at(q) == true);
    REQUIRE(samples[3].at(q) == false);
  }
}

TEST_CASE("Bit-parallel and golden sample the same D at an edge that changes it",
          "[sequential][edge]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dff_fanout.json");
  TestVector vec;
  vec.cycles = {at(false, false), at(true, true)};
  GoldenRefSim golden;
  BitParallelSim parallel;
  const auto good = golden.simulate_sequence_fault_free(cg, vec);
  for (const int net : {8, 9, kD, kE}) {
    for (const FaultType type : {FaultType::SA0, FaultType::SA1}) {
      CompactFault fault;
      fault.net_index = cg.yosys_to_compiled.at(net);
      fault.type = type;
      const bool detected = golden.is_sequence_detected(
          cg, good, golden.simulate_sequence_with_fault(cg, vec, fault));
      INFO("net " << net << " sa" << (type == FaultType::SA1 ? 1 : 0));
      REQUIRE(parallel.simulate_single_fault(cg, vec, fault) == detected);
    }
  }
}

TEST_CASE("The first cycle's edge samples D as it is: nothing settled before it",
          "[sequential][edge]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dff_fanout.json");
  TestVector vec;
  vec.cycles = {at(true, false)};  // a clock high from the start, D 0 -> DN 1
  GoldenRefSim golden;
  const auto samples = golden.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.at(0).at(kQ0) == true);
  REQUIRE(samples.at(0).at(kQ2) == true);
}
