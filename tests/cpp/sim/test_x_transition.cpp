// Stage 7 of the X engine: transition faults. A transition is launched only when
// the good machine at the fault site is known at its pre-transition value in the
// launch frame and at its post-transition value in the capture frame; given that,
// the capture-frame stuck-at is graded definite or possible as a stuck-at is.
// Nothing is credited where the launch itself is unknown.
//
// tiny_xbit: y = a & (c | x), n1 = c | x, x an "x" bit of the netlist.
#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <filesystem>
#include <map>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/xsim/bit_parallel_sim_x.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

std::vector<std::string> loadable_fixtures() {
  std::vector<std::string> out;
  for (const auto& entry : std::filesystem::directory_iterator(FAULTFLOW_FIXTURE_DIR)) {
    const std::string name = entry.path().filename().string();
    if (name.rfind("tiny_", 0) != 0 || entry.path().extension() != ".json") {
      continue;
    }
    try {
      (void)test::load_compiled(name);
      out.push_back(name);
    } catch (const std::exception&) {
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

std::vector<FaultBatch> batches_of(std::vector<CompactFault> faults) {
  std::vector<FaultBatch> out;
  for (size_t i = 0; i < faults.size(); i += kBatchSize) {
    FaultBatch batch;
    for (size_t j = i; j < std::min(faults.size(), i + kBatchSize); ++j) {
      CompactFault f = faults[j];
      f.exclusion = FaultExclusion::NONE;
      f.bit = static_cast<uint8_t>(batch.size + 1);
      f.sa_mask = 1ULL << f.bit;
      batch.faults[batch.size++] = f;
      batch.mask |= f.sa_mask;
    }
    out.push_back(batch);
  }
  return out;
}

struct Pair {
  TestVector v1;
  TestVector v2;
  XRunOptions options;
};

Pair random_pair(const CompiledSimGraph& cg, std::mt19937& rng, bool with_x) {
  Pair p;
  for (TestVector* v : {&p.v1, &p.v2}) {
    std::set<int> unknown;
    for (int pi : cg.pi_nets) {
      const int yid = cg.compiled_to_yosys[pi];
      v->inputs[yid] = (rng() & 1) != 0;
      if (with_x && rng() % 5 == 0) {
        unknown.insert(yid);
      }
    }
    p.options.unknown_inputs.push_back(unknown);
  }
  p.options.initial_state = with_x ? Logic::X : Logic::ZERO;
  return p;
}

const std::string& xbit() {
  static const std::string path = test::fixture_path("tiny_xbit.json");
  return path;
}

const CachedGraph& load(const std::string& fixture, const std::string& policy) {
  return load_cached_graph(fixture, test::cell_map_path(), policy);
}

CompactFault fault_on(const CachedGraph& g, const std::string& net, FaultType type) {
  CompactFault f;
  f.net_index =
      static_cast<uint32_t>(g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name(net)));
  f.type = type;
  f.exclusion = FaultExclusion::NONE;
  return f;
}

TestVector inputs(const CachedGraph& g, const std::map<std::string, bool>& values) {
  TestVector vec;
  for (const auto& [name, value] : values) {
    vec.inputs[g.parsed.net_id_by_name(name)] = value;
  }
  return vec;
}

TestVector inputs(const CachedGraph& g, const std::vector<AtpgPiInfo>& pis, unsigned bits) {
  TestVector vec;
  for (size_t i = 0; i < pis.size(); ++i) {
    vec.inputs[g.parsed.net_id_by_name(pis[i].name)] = ((bits >> i) & 1U) != 0;
  }
  return vec;
}

SatSolveOptions unlimited() {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  return options;
}

XRunOptions powered_up_unknown() {
  XRunOptions options;
  options.initial_state = Logic::X;
  return options;
}

bool has_loop_or_bus(const CompiledSimGraph& cg) {
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const GateType t = cg.nodes[i].type;
    if ((i < cg.in_loop.size() && cg.in_loop[i] != 0) || t == GateType::TBUF_P ||
        t == GateType::TBUF_N || t == GateType::TINV_P || t == GateType::TINV_N ||
        t == GateType::BUS_MERGE || t == GateType::BUS_RES) {
      return true;
    }
  }
  return false;
}

}  // namespace

TEST_CASE("The X transition grade equals the scalar X oracle on every fault",
          "[xsim][xtransition]") {
  const auto fixtures = loadable_fixtures();
  REQUIRE(fixtures.size() > 30);
  const GoldenRefSimX ref;
  const BitParallelSimX sim;
  std::mt19937 rng(20261113);
  int definite = 0;
  int possible = 0;
  for (const std::string& name : fixtures) {
    INFO(name);
    const NormalizedGraph ng = test::load_normalized(name);
    const CompiledSimGraph cg = test::load_compiled(name);
    for (const FaultBatch& batch : batches_of(enumerate_transition_faults(ng, cg))) {
      for (int k = 0; k < 6; ++k) {
        const Pair p = random_pair(cg, rng, true);
        const XBatchDetection det =
            sim.detect_transition_batch(cg, p.v1, p.v2, batch, p.options);
        REQUIRE((det.definite & ~batch.mask) == 0ULL);
        REQUIRE((det.definite & det.possible) == 0ULL);
        for (int b = 1; b <= batch.size; ++b) {
          const XDetection want =
              ref.simulate_transition_fault(cg, p.v1, p.v2, batch.faults[b - 1], p.options);
          const uint64_t bit = 1ULL << b;
          REQUIRE(((det.definite & bit) != 0) == (want == XDetection::DEFINITE));
          REQUIRE(((det.possible & bit) != 0) == (want == XDetection::POSSIBLE));
          definite += want == XDetection::DEFINITE ? 1 : 0;
          possible += want == XDetection::POSSIBLE ? 1 : 0;
        }
      }
    }
  }
  CHECK(definite > 0);
  CHECK(possible > 0);
}

TEST_CASE("With no X source the X transition grade is the two-valued one",
          "[xsim][xtransition]") {
  const GoldenRefSim golden;
  const GoldenRefSimX ref;
  const BitParallelSim two;
  const BitParallelSimX three;
  std::mt19937 rng(7);
  for (const std::string& name : loadable_fixtures()) {
    INFO(name);
    const NormalizedGraph ng = test::load_normalized(name);
    const CompiledSimGraph cg = test::load_compiled(name);
    for (const FaultBatch& batch : batches_of(enumerate_transition_faults(ng, cg))) {
      for (int k = 0; k < 4; ++k) {
        const Pair p = random_pair(cg, rng, false);
        const XBatchDetection det =
            three.detect_transition_batch(cg, p.v1, p.v2, batch, p.options);
        REQUIRE(det.definite == two.simulate_transition_batch(cg, p.v1, p.v2, batch));
        REQUIRE(det.possible == 0ULL);
        for (int b = 1; b <= batch.size; ++b) {
          const CompactFault& f = batch.faults[b - 1];
          const bool want = golden.simulate_transition_fault(cg, p.v1, p.v2, f);
          REQUIRE((ref.simulate_transition_fault(cg, p.v1, p.v2, f, p.options) ==
                   XDetection::DEFINITE) == want);
        }
      }
    }
  }
}

TEST_CASE("A transition whose launch value is unknown gets no credit",
          "[xmode][xtransition]") {
  const CachedGraph& x = load(xbit(), "fail+x");
  const CachedGraph& two = load(xbit(), "fail");
  const GoldenRefSimX ref;
  const GoldenRefSim golden;
  const XRunOptions options = powered_up_unknown();
  // n1 slow to rise, launch c = 0 and capture c = 1 (a = 1). Two-valued (x tied
  // 0) n1 = c rises and its stuck-at 0 shows at y. With x unknown n1 is X in the
  // launch frame: the rise may never happen, so nothing is credited -- though
  // its stuck-at 0 alone is a definite detection in the capture frame.
  const TestVector launch = inputs(x, {{"a", true}, {"c", false}});
  const TestVector capture = inputs(x, {{"a", true}, {"c", true}});
  CHECK(golden.simulate_transition_fault(two.cg, launch, capture,
                                         fault_on(two, "n1", FaultType::SA0)));
  CHECK(ref.simulate_transition_fault(x.cg, launch, capture,
                                      fault_on(x, "n1", FaultType::SA0),
                                      options) == XDetection::NONE);
  const CompactFault n1_sa0 = fault_on(x, "n1", FaultType::SA0);
  CHECK(classify_detection(x.cg, ref.simulate_sequence(x.cg, capture, nullptr, options),
                           ref.simulate_sequence(x.cg, capture, &n1_sa0, options)) ==
        XDetection::DEFINITE);
  // y rises with a: a definite detection.
  CHECK(ref.simulate_transition_fault(x.cg, inputs(x, {{"a", false}, {"c", true}}),
                                      capture, fault_on(x, "y", FaultType::SA0),
                                      options) == XDetection::DEFINITE);
  // c rises: the launch is known, but the faulty c = 0 leaves y = a & X unknown
  // where the good y is 1 -- a possible detection.
  CHECK(ref.simulate_transition_fault(x.cg, launch, capture,
                                      fault_on(x, "c", FaultType::SA0),
                                      options) == XDetection::POSSIBLE);
  // a rises with c = 0: a's launch is known, but the good y is X -- no credit.
  CHECK(ref.simulate_transition_fault(x.cg, inputs(x, {{"a", false}, {"c", false}}),
                                      inputs(x, {{"a", true}, {"c", false}}),
                                      fault_on(x, "a", FaultType::SA0),
                                      options) == XDetection::NONE);
}

TEST_CASE("Dual-rail transition SAT: a definite test, a possible one",
          "[xmode][xtransition][atpg]") {
  const CachedGraph& x = load(xbit(), "fail+x");
  const auto pis = ordered_pis(x.parsed, x.cg);
  const GoldenRefSimX ref;
  const XRunOptions options = powered_up_unknown();
  std::map<std::string, bool> v1;
  std::map<std::string, bool> v2;
  // y slow to rise: a definite test.
  const CompactFault y_str = fault_on(x, "y", FaultType::SA0);
  REQUIRE(solve_transition_fault_x(x.cg, pis, y_str, unlimited(), v1, v2) ==
          SatSolveResult::SAT);
  CHECK(ref.simulate_transition_fault(x.cg, inputs(x, v1), inputs(x, v2), y_str,
                                      options) == XDetection::DEFINITE);
  // c slow to rise: no definite test, one that shows it for some value of x.
  const CompactFault c_str = fault_on(x, "c", FaultType::SA0);
  REQUIRE(solve_transition_fault_x(x.cg, pis, c_str, unlimited(), v1, v2) ==
          SatSolveResult::POSSIBLE);
  CHECK(ref.simulate_transition_fault(x.cg, inputs(x, v1), inputs(x, v2), c_str,
                                      options) == XDetection::POSSIBLE);
  // n1 slow to rise: its launch is never known, no definite test -- but some
  // value of x launches and shows it, so it is not redundant.
  const CompactFault n1_str = fault_on(x, "n1", FaultType::SA0);
  CHECK(solve_transition_fault_x(x.cg, pis, n1_str, unlimited(), v1, v2) ==
        SatSolveResult::POSSIBLE);
}

TEST_CASE("Dual-rail transition SAT agrees with the X grade of every pair",
          "[xmode][xtransition][atpg]") {
  // Every fault of the X fixtures, every launch/capture pair: a SAT pair is a
  // definite detection, and SAT exactly when some pair is one (no loop or bus:
  // the dual-rail CNF is then the X engine's own evaluation). UNSAT (redundant)
  // only where no pair is definite. (A pair may still be a possible detection
  // of a redundant fault: X pessimism, correlated unknowns read independently.)
  const GoldenRefSimX ref;
  const XRunOptions options = powered_up_unknown();
  int sat = 0;
  int possible = 0;
  for (const std::string name :
       {"tiny_xbit.json", "tiny_xlatch.json", "tiny_loop_sr.json", "tiny_tbus.json"}) {
    INFO(name);
    const CachedGraph& g = load(test::fixture_path(name), "fail+x");
    const auto pis = ordered_pis(g.parsed, g.cg);
    REQUIRE(pis.size() <= 6);
    const bool exact = !has_loop_or_bus(g.cg);
    const unsigned span = 1U << pis.size();
    for (const CompactFault& f : enumerate_transition_faults(g.ng, g.cg)) {
      if (f.exclusion != FaultExclusion::NONE) {
        continue;
      }
      INFO("net " << f.net_index << (f.type == FaultType::SA0 ? " str" : " stf"));
      XDetection best = XDetection::NONE;
      for (unsigned a = 0; a < span && best != XDetection::DEFINITE; ++a) {
        for (unsigned b = 0; b < span && best != XDetection::DEFINITE; ++b) {
          const XDetection d = ref.simulate_transition_fault(
              g.cg, inputs(g, pis, a), inputs(g, pis, b), f, options);
          best = std::max(best, d);
        }
      }
      std::map<std::string, bool> v1;
      std::map<std::string, bool> v2;
      const SatSolveResult r = solve_transition_fault_x(g.cg, pis, f, unlimited(), v1, v2);
      if (r == SatSolveResult::SAT) {
        ++sat;
        if (exact) {
          CHECK(ref.simulate_transition_fault(g.cg, inputs(g, v1), inputs(g, v2), f,
                                              options) == XDetection::DEFINITE);
        }
      }
      if (exact) {
        CHECK((r == SatSolveResult::SAT) == (best == XDetection::DEFINITE));
      }
      if (r == SatSolveResult::UNSAT) {
        CHECK(best != XDetection::DEFINITE);
      }
      possible += r == SatSolveResult::POSSIBLE ? 1 : 0;
    }
  }
  CHECK(sat > 0);
  CHECK(possible > 0);
}

TEST_CASE("Dual-rail scan LOC and LOS SAT agree with the X grade of every launch",
          "[xmode][xtransition][atpg]") {
  // A pseudo scan view (the coupling is all the solver sees of one): the first
  // input a pseudo-input, its capture value the first output's launch-frame
  // value (LOC; unknown where that is X) or a free launch-shift bit (LOS, the
  // chain head); every other input held. Each fault, every launch: a SAT result
  // is a definite detection, SAT exactly when some launch is one (no loop or
  // bus), UNSAT only where none is.
  const GoldenRefSimX ref;
  const XRunOptions power_up = powered_up_unknown();
  int sat = 0;
  for (const std::string name : {"tiny_xlatch.json", "tiny_loop_sr.json", "tiny_tbus.json"}) {
    INFO(name);
    const CachedGraph& g = load(test::fixture_path(name), "fail+x");
    const auto pis = ordered_pis(g.parsed, g.cg);
    REQUIRE(pis.size() >= 2);
    REQUIRE_FALSE(g.cg.observable.empty());
    const bool exact = !has_loop_or_bus(g.cg);
    const uint32_t ppi = pis[0].compiled;
    const auto ppo = static_cast<uint32_t>(g.cg.observable[0]);
    std::vector<uint32_t> held;
    for (size_t i = 1; i < pis.size(); ++i) {
      held.push_back(pis[i].compiled);
    }
    const int ppi_yid = g.cg.compiled_to_yosys[ppi];
    const int ppo_yid = g.cg.compiled_to_yosys[ppo];
    const unsigned span = 1U << pis.size();
    for (const bool los : {false, true}) {
      INFO((los ? "LOS" : "LOC"));
      const auto grade = [&](const CompactFault& f, unsigned launch, bool head) {
        const TestVector v1 = inputs(g, pis, launch);
        TestVector v2 = v1;
        XRunOptions options = power_up;
        if (los) {
          v2.inputs[ppi_yid] = head;
        } else {
          const Logic next =
              ref.simulate_sequence(g.cg, v1, nullptr, power_up).at(0).at(ppo_yid);
          v2.inputs[ppi_yid] = next == Logic::ONE;
          options.unknown_inputs = {{}, {}};
          if (!is_known(next)) {
            options.unknown_inputs[1].insert(ppi_yid);
          }
        }
        return ref.simulate_transition_fault(g.cg, v1, v2, f, options);
      };
      for (const CompactFault& f : enumerate_transition_faults(g.ng, g.cg)) {
        if (f.exclusion != FaultExclusion::NONE) {
          continue;
        }
        INFO("net " << f.net_index << (f.type == FaultType::SA0 ? " str" : " stf"));
        XDetection best = XDetection::NONE;
        for (unsigned a = 0; a < span && best != XDetection::DEFINITE; ++a) {
          for (const bool head : {false, true}) {
            if (!los && head) {
              continue;
            }
            best = std::max(best, grade(f, a, head));
          }
        }
        std::map<std::string, bool> v1;
        std::map<std::string, bool> v2;
        const SatSolveResult r =
            los ? solve_scan_los_transition_fault_x(g.cg, pis, {}, {ppi}, held, f,
                                                    unlimited(), v1, v2)
                : solve_scan_transition_fault_x(g.cg, pis, {{ppi, ppo}}, held, f,
                                                unlimited(), v1, v2);
        if (r == SatSolveResult::SAT) {
          ++sat;
          unsigned launch = 0;
          for (size_t i = 0; i < pis.size(); ++i) {
            launch |= v1.at(pis[i].name) ? 1U << i : 0U;
          }
          if (exact) {
            CHECK(grade(f, launch, v2.at(pis[0].name)) == XDetection::DEFINITE);
          }
        }
        if (exact) {
          CHECK((r == SatSolveResult::SAT) == (best == XDetection::DEFINITE));
        }
        if (r == SatSolveResult::UNSAT) {
          CHECK(best != XDetection::DEFINITE);
        }
      }
    }
  }
  CHECK(sat > 0);
}

TEST_CASE("With no unknown in its cones the X transition SAT is the two-valued one",
          "[xmode][xtransition][atpg]") {
  int compared = 0;
  for (const std::string& name : loadable_fixtures()) {
    const CachedGraph* g = nullptr;
    try {
      g = &load(test::fixture_path(name), "fail+x");
    } catch (const std::exception&) {
      continue;
    }
    bool unknown = false;
    for (const SimNode& node : g->cg.nodes) {
      unknown = unknown || node.type == GateType::CONSTX || node.type == GateType::LATCH;
    }
    if (unknown || has_loop_or_bus(g->cg) || g->cg.observable.empty()) {
      continue;
    }
    INFO(name);
    const auto pis = ordered_pis(g->parsed, g->cg);
    for (const CompactFault& f : enumerate_transition_faults(g->ng, g->cg)) {
      std::map<std::string, bool> a1;
      std::map<std::string, bool> a2;
      std::map<std::string, bool> b1;
      std::map<std::string, bool> b2;
      SatSolveResult want = SatSolveResult::UNKNOWN;
      try {
        want = solve_transition_fault(g->cg, pis, f, unlimited(), a1, a2);
      } catch (const std::exception&) {
        // A cell the CNF does not encode (a clock gate): refused by both.
        CHECK_THROWS(solve_transition_fault_x(g->cg, pis, f, unlimited(), b1, b2));
        continue;
      }
      const SatSolveResult got = solve_transition_fault_x(g->cg, pis, f, unlimited(), b1, b2);
      REQUIRE(got == want);
      CHECK(b1 == a1);
      CHECK(b2 == a2);
      ++compared;
    }
  }
  CHECK(compared > 100);
}
