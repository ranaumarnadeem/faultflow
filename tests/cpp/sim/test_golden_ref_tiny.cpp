#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

TEST_CASE("GoldenRefSim tiny_inv detection", "[golden_ref][tiny_fixtures]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int po = pg.net_id_by_name("Y");
  GoldenRefSim sim;

  TestVector v0;
  v0.inputs[pg.net_id_by_name("A")] = false;
  const auto ff = sim.simulate_fault_free(cg, v0);
  REQUIRE(ff.at(po) == true);

  CompactFault sa0;
  sa0.net_index = cg.yosys_to_compiled.at(po);
  sa0.type = FaultType::SA0;
  const auto faulty0 = sim.simulate_with_fault(cg, v0, sa0);
  REQUIRE(sim.is_detected(cg, ff, faulty0));

  CompactFault sa1;
  sa1.net_index = cg.yosys_to_compiled.at(po);
  sa1.type = FaultType::SA1;
  const auto faulty1a = sim.simulate_with_fault(cg, v0, sa1);
  REQUIRE_FALSE(sim.is_detected(cg, ff, faulty1a));

  TestVector v1;
  v1.inputs[pg.net_id_by_name("A")] = true;
  const auto ff1 = sim.simulate_fault_free(cg, v1);
  const auto faulty1b = sim.simulate_with_fault(cg, v1, sa1);
  REQUIRE(sim.is_detected(cg, ff1, faulty1b));
}
