#include <catch2/catch_test_macros.hpp>

#include <map>
#include <string>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

// M2 merge gate: cone-of-influence restriction is provably verdict-preserving,
// so for EVERY enumerated stuck-at fault the cone-restricted solver must return
// the SAME SAT/UNSAT verdict as the whole-circuit solver. Unlimited budget
// (no conflict/time limit) makes every fault resolve deterministically to
// SAT or UNSAT, so there is no TIMEOUT nondeterminism to confound the compare.
void check_cone_equivalence(const std::string& rel) {
  const ParsedGraph pg = test::load_parsed_benchmark(rel);
  const NormalizedGraph ng = test::load_normalized_benchmark(rel);
  const CompiledSimGraph cg = test::load_compiled_benchmark(rel);
  const auto pis = ordered_pis(pg, cg);
  const auto faults = enumerate_faults(ng, cg);

  SatSolveOptions full;
  full.conflict_limit = -1;
  full.sat_timeout_seconds = 0;
  full.cone_restrict = false;
  SatSolveOptions cone = full;
  cone.cone_restrict = true;

  size_t checked = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    std::map<std::string, bool> v_full;
    std::map<std::string, bool> v_cone;
    const SatSolveResult r_full =
        solve_stuck_at_fault(cg, pis, fault, full, v_full);
    const SatSolveResult r_cone =
        solve_stuck_at_fault(cg, pis, fault, cone, v_cone);
    INFO("fault net_index=" << fault.net_index
                            << " type=" << static_cast<int>(fault.type));
    REQUIRE(r_full == r_cone);
    ++checked;
  }
  REQUIRE(checked > 0);
}

}  // namespace

TEST_CASE("Cone SAT verdict == whole-circuit on c17",
          "[atpg][cone][equivalence]") {
  check_cone_equivalence("iscas85/synth_sky130/c17.json");
}

TEST_CASE("Cone SAT verdict == whole-circuit on c432",
          "[atpg][cone][equivalence]") {
  check_cone_equivalence("iscas85/synth_sky130/c432.json");
}

TEST_CASE("Cone SAT verdict == whole-circuit on c499",
          "[atpg][cone][equivalence]") {
  check_cone_equivalence("iscas85/synth_sky130/c499.json");
}
