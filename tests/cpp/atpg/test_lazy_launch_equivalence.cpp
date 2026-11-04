#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <map>
#include <string>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

// Lazy launch frame (SatSolveOptions::lazy_launch): a two-frame transition CNF
// encodes the launch frame over the nets the fault site and the capture frame
// read only. For every enumerated transition fault the verdict must equal the
// full launch frame's, with and without the capture-frame cone, and every SAT
// pair must be a real test. The scan views are pseudo ones built on ISCAS85
// netlists (the coupling is all the solver sees of a scan view): LOC couples
// the first k PIs to the first k observables and holds the rest; LOS chains
// the first k PIs, the first being the chain head.

namespace {

enum class Kind { BROADSIDE, LOC, LOS };

SatSolveOptions unlimited(bool lazy, bool cone) {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  options.lazy_launch = lazy;
  options.cone_restrict = cone;
  return options;
}

TestVector vector_from_map(const ParsedGraph& pg,
                           const std::map<std::string, bool>& values) {
  TestVector vec;
  for (const auto& [name, value] : values) {
    vec.inputs[pg.net_id_by_name(name)] = value;
  }
  return vec;
}

struct PseudoView {
  std::vector<LocCouple> loc;
  std::vector<LosCouple> los;
  std::vector<uint32_t> heads;
  std::vector<uint32_t> held;
};

PseudoView pseudo_view(const CompiledSimGraph& cg,
                       const std::vector<AtpgPiInfo>& pis) {
  PseudoView view;
  const size_t k = std::min(pis.size(), cg.observable.size()) / 2;
  REQUIRE(k >= 1);
  for (size_t i = 0; i < k; ++i) {
    view.loc.push_back(
        {pis[i].compiled, static_cast<uint32_t>(cg.observable[i])});
    if (i == 0) {
      view.heads.push_back(pis[i].compiled);
    } else {
      view.los.push_back({pis[i].compiled, pis[i - 1].compiled});
    }
  }
  for (size_t i = k; i < pis.size(); ++i) {
    view.held.push_back(pis[i].compiled);
  }
  return view;
}

SatSolveResult solve(Kind kind, const CompiledSimGraph& cg,
                     const std::vector<AtpgPiInfo>& pis, const PseudoView& view,
                     const CompactFault& fault, const SatSolveOptions& options,
                     std::map<std::string, bool>& v1,
                     std::map<std::string, bool>& v2) {
  switch (kind) {
    case Kind::BROADSIDE:
      return solve_transition_fault(cg, pis, fault, options, v1, v2);
    case Kind::LOC:
      return solve_scan_transition_fault(cg, pis, view.loc, view.held, fault,
                                         options, v1, v2);
    case Kind::LOS:
      return solve_scan_los_transition_fault(cg, pis, view.los, view.heads,
                                             view.held, fault, options, v1, v2);
  }
  return SatSolveResult::UNKNOWN;
}

// The pair is a test, and V2 follows from V1 as the coupling says.
void require_test(Kind kind, const ParsedGraph& pg, const CompiledSimGraph& cg,
                  const std::vector<AtpgPiInfo>& pis, const PseudoView& view,
                  const CompactFault& fault,
                  const std::map<std::string, bool>& v1,
                  const std::map<std::string, bool>& v2) {
  GoldenRefSim golden;
  const TestVector tv1 = vector_from_map(pg, v1);
  REQUIRE(golden.simulate_transition_fault(cg, tv1, vector_from_map(pg, v2),
                                           fault));
  std::map<uint32_t, std::string> name_of;
  for (const AtpgPiInfo& pi : pis) {
    name_of[pi.compiled] = pi.name;
  }
  if (kind == Kind::BROADSIDE) {
    return;  // launch and capture independent
  }
  for (uint32_t held : view.held) {
    REQUIRE(v2.at(name_of.at(held)) == v1.at(name_of.at(held)));
  }
  if (kind == Kind::LOC) {
    const auto launch = golden.simulate_fault_free(cg, tv1);
    for (const LocCouple& c : view.loc) {
      REQUIRE(v2.at(name_of.at(c.ppi_compiled)) ==
              launch.at(cg.compiled_to_yosys.at(c.ppo_compiled)));
    }
  } else {
    for (const LosCouple& c : view.los) {
      REQUIRE(v2.at(name_of.at(c.capture_ppi_compiled)) ==
              v1.at(name_of.at(c.pred_ppi_compiled)));
    }
  }
}

// The SAT count (the cone_restrict runs).
size_t check_lazy_launch(Kind kind, const std::string& rel) {
  const ParsedGraph pg = test::load_parsed_benchmark(rel);
  const NormalizedGraph ng = test::load_normalized_benchmark(rel);
  const CompiledSimGraph cg = test::load_compiled_benchmark(rel);
  const auto pis = ordered_pis(pg, cg);
  const PseudoView view = pseudo_view(cg, pis);
  const auto faults = enumerate_transition_faults(ng, cg);

  size_t sat = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    INFO("transition fault net_index=" << fault.net_index
                                       << " type=" << static_cast<int>(fault.type));
    for (bool cone : {false, true}) {
      std::map<std::string, bool> f1, f2, l1, l2;
      const SatSolveResult full =
          solve(kind, cg, pis, view, fault, unlimited(false, cone), f1, f2);
      const SatSolveResult lazy =
          solve(kind, cg, pis, view, fault, unlimited(true, cone), l1, l2);
      INFO("cone_restrict=" << cone);
      REQUIRE(full == lazy);
      if (lazy == SatSolveResult::SAT) {
        require_test(kind, pg, cg, pis, view, fault, l1, l2);
      }
      if (cone && lazy == SatSolveResult::SAT) {
        ++sat;
      }
    }
  }
  return sat;
}

void check_lazy_launch(Kind kind) {
  size_t sat = 0;
  for (const char* rel : {"iscas85/synth_sky130/c17.json",
                          "iscas85/synth_sky130/c432.json",
                          "iscas85/synth_sky130/c499.json"}) {
    sat += check_lazy_launch(kind, rel);
  }
  REQUIRE(sat > 0);
}

}  // namespace

TEST_CASE("Lazy launch frame: broadside verdicts equal the full frame's",
          "[atpg][transition][lazy_launch][equivalence]") {
  check_lazy_launch(Kind::BROADSIDE);
}

TEST_CASE("Lazy launch frame: LOC verdicts equal the full frame's, V2 = f(V1)",
          "[atpg][transition][lazy_launch][equivalence]") {
  check_lazy_launch(Kind::LOC);
}

TEST_CASE("Lazy launch frame: LOS verdicts equal the full frame's",
          "[atpg][transition][lazy_launch][equivalence]") {
  check_lazy_launch(Kind::LOS);
}
