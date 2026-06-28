#include <catch2/catch_test_macros.hpp>

#include <map>
#include <string>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

// IFC (Tille et al. incremental fan-in cones) must be verdict-identical to the
// single-shot cone-restricted solver: for EVERY enumerated stuck-at fault both
// must return the same SAT/UNSAT classification (I1). Unlimited budget removes
// TIMEOUT nondeterminism. When both say SAT, the IFC vector must actually detect
// the fault in the golden simulator (I2) — a SAT result that does not detect
// would be a soundness bug in the incremental miter.
void check_ifc_equivalence(const std::string& rel, int fup_region_budget) {
  const ParsedGraph pg = test::load_parsed_benchmark(rel);
  const NormalizedGraph ng = test::load_normalized_benchmark(rel);
  const CompiledSimGraph cg = test::load_compiled_benchmark(rel);
  const auto pis = ordered_pis(pg, cg);
  const auto faults = enumerate_faults(ng, cg);

  SatSolveOptions base;
  base.conflict_limit = -1;
  base.sat_timeout_seconds = 0;
  base.cone_restrict = true;  // the production baseline path
  // Drives the IFC FUP region size: a small budget forces FUP to fire on nearly
  // every fault, stress-testing its redundancy proof; 0 disables FUP.
  base.fup_region_budget = fup_region_budget;

  const GoldenRefSim ref;
  size_t checked = 0;
  size_t sat = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    std::map<std::string, bool> v_base;
    std::map<std::string, bool> v_ifc;
    const SatSolveResult r_base =
        solve_stuck_at_fault(cg, pis, fault, base, v_base);
    const SatSolveResult r_ifc =
        solve_stuck_at_fault_incremental(cg, pis, fault, base, v_ifc);
    INFO("fault net_index=" << fault.net_index
                            << " type=" << static_cast<int>(fault.type));
    REQUIRE(r_base == r_ifc);
    if (r_ifc == SatSolveResult::SAT) {
      // I2: the incremental vector must detect the fault in the golden sim.
      TestVector tv;
      for (const AtpgPiInfo& pi : pis) {
        tv.inputs[pi.yosys_id] = v_ifc.at(pi.name);
      }
      const auto ff = ref.simulate_fault_free(cg, tv);
      const auto fy = ref.simulate_with_fault(cg, tv, fault);
      REQUIRE(ref.is_detected(cg, ff, fy));
      ++sat;
    }
    ++checked;
  }
  REQUIRE(checked > 0);
  REQUIRE(sat > 0);  // the design has testable faults, so IFC must produce vectors
}

}  // namespace

TEST_CASE("IFC SAT verdict == baseline on c17", "[atpg][ifc][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c17.json", 32);
}

TEST_CASE("IFC SAT verdict == baseline on c432", "[atpg][ifc][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c432.json", 32);
}

TEST_CASE("IFC SAT verdict == baseline on c499", "[atpg][ifc][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c499.json", 32);
}

// A tiny FUP region budget forces the fast-untestability-proof path on nearly
// every multi-gate fault cone, so its bounded redundancy proof is stress-tested
// against the baseline verdict (a false-redundant FUP would diverge here).
TEST_CASE("IFC+aggressive-FUP verdict == baseline on c432",
          "[atpg][ifc][fup][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c432.json", 4);
}

TEST_CASE("IFC+aggressive-FUP verdict == baseline on c499",
          "[atpg][ifc][fup][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c499.json", 4);
}

// FUP disabled (budget 0) must still be verdict-identical: the IFC sweep alone
// classifies every fault.
TEST_CASE("IFC without FUP verdict == baseline on c432",
          "[atpg][ifc][equivalence]") {
  check_ifc_equivalence("iscas85/synth_sky130/c432.json", 0);
}
