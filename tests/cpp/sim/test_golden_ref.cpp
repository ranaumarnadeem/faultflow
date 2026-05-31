#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

TEST_CASE("GoldenRefSim c17 fault-free", "[golden_ref]") {
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  const ParsedGraph pg =
      test::load_parsed_benchmark("iscas85/synth/c17.json");
  GoldenRefSim sim;

  TestVector v0;
  for (const int pi : {2, 3, 4, 5, 6}) {
    v0.inputs[pi] = false;
  }
  const int n22 = pg.net_id_by_name("N22");
  const auto ff = sim.simulate_fault_free(cg, v0);
  REQUIRE(ff.count(n22) == 1);
}

TEST_CASE("GoldenRefSim exhaustive c17", "[golden_ref]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  const auto faults = enumerate_faults(ng, cg);
  std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
  std::sort(pi_ids.begin(), pi_ids.end());
  const test::VectorSet vs = test::generate_complete_input_space(pi_ids);
  REQUIRE(vs.vectors.size() == 32);
  REQUIRE(faults.size() >= 2);
}
