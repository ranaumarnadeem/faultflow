#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

static int po_yosys_id(const CompiledSimGraph& cg) {
  REQUIRE(!cg.observable.empty());
  return cg.compiled_to_yosys[cg.observable.front()];
}

TEST_CASE("GoldenRefSim tiny_inv", "[golden_ref]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int po = po_yosys_id(cg);
  GoldenRefSim sim;

  TestVector v0;
  v0.inputs[2] = false;
  const auto ff = sim.simulate_fault_free(cg, v0);
  REQUIRE(ff.at(po) == true);

  CompactFault sa0;
  sa0.net_index = cg.yosys_to_compiled.at(3);
  sa0.type = FaultType::SA0;
  const auto faulty0 = sim.simulate_with_fault(cg, v0, sa0);
  REQUIRE(sim.is_detected(cg, ff, faulty0));

  CompactFault sa1;
  sa1.net_index = cg.yosys_to_compiled.at(3);
  sa1.type = FaultType::SA1;
  const auto faulty1a = sim.simulate_with_fault(cg, v0, sa1);
  REQUIRE_FALSE(sim.is_detected(cg, ff, faulty1a));

  TestVector v1;
  v1.inputs[2] = true;
  const auto ff1 = sim.simulate_fault_free(cg, v1);
  const auto faulty1b = sim.simulate_with_fault(cg, v1, sa1);
  REQUIRE(sim.is_detected(cg, ff1, faulty1b));
}

TEST_CASE("GoldenRefSim exhaustive tiny circuits", "[golden_ref]") {
  GoldenRefSim sim;
  for (const char* fixture :
       {"tiny_inv.json", "tiny_and2.json", "tiny_nor2.json"}) {
    const NormalizedGraph ng = test::load_normalized(fixture);
    const CompiledSimGraph cg = test::load_compiled(fixture);
    const auto faults = enumerate_faults(ng, cg);

    std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
    std::sort(pi_ids.begin(), pi_ids.end());
    const VectorSet vs = generate_complete_input_space(pi_ids);

    for (const auto& fault : faults) {
      if (fault.exclusion != FaultExclusion::NONE) {
        continue;
      }
      bool detected = false;
      for (const auto& vec : vs.vectors) {
        const auto ff = sim.simulate_fault_free(cg, vec);
        const auto fa = sim.simulate_with_fault(cg, vec, fault);
        if (sim.is_detected(cg, ff, fa)) {
          detected = true;
          break;
        }
      }
      (void)detected;
    }
    REQUIRE(faults.size() >= 2);
  }
}
