#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"

using namespace faultflow;

TEST_CASE("BitParallelSim matches GoldenRefSim on c17", "[bit_parallel]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const auto faults = enumerate_faults(ng, cg);
  std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
  std::sort(pi_ids.begin(), pi_ids.end());
  const test::VectorSet vs = test::generate_complete_input_space(pi_ids);
  const auto mismatches =
      test::verify_parallel_matches_golden(cg, ng, faults, vs);
  REQUIRE(mismatches.empty());
}

TEST_CASE("BitParallelSim bit-0 invariant c17", "[bit_parallel]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  BitParallelSim sim;
  SimState state;
  state.init(cg.net_count);

  TestVector vec;
  for (const int pi : {2, 3, 4, 5, 6}) {
    vec.inputs[pi] = true;
  }
  FaultBatch batch;
  batch.size = 0;
  batch.mask = 0;
  sim.broadcast_inputs(state, cg, vec);
  sim.evaluate_combinational(state, cg, batch);
  for (int obs : cg.observable) {
    REQUIRE((state.current_values()[obs] & 1ULL) ==
            (state.current_values()[obs] != 0));
  }
}
