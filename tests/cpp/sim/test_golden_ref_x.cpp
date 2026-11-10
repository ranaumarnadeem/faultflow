#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <filesystem>
#include <random>
#include <string>
#include <vector>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

using namespace faultflow;

namespace {

constexpr Logic L0 = Logic::ZERO;
constexpr Logic L1 = Logic::ONE;
constexpr Logic LX = Logic::X;

TestCycle cycle(std::initializer_list<std::pair<const int, bool>> inputs,
                bool sample = true) {
  TestCycle c;
  c.inputs = std::map<int, bool>(inputs);
  c.sample_outputs = sample;
  return c;
}

CompactFault fault_on(const CompiledSimGraph& cg, int yid, FaultType type) {
  CompactFault f;
  f.net_index = static_cast<uint32_t>(cg.yosys_to_compiled.at(yid));
  f.type = type;
  f.exclusion = FaultExclusion::NONE;
  return f;
}

std::map<int, Logic> as_logic(const std::map<int, bool>& snap) {
  std::map<int, Logic> out;
  for (const auto& [yid, v] : snap) {
    out[yid] = v ? L1 : L0;
  }
  return out;
}

// The tiny fixtures the two-valued flow loads (latch, tristate and unknown-cell
// fixtures throw, as they should).
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

}  // namespace

TEST_CASE("With no X source the X reference equals the two-valued one, cycle by cycle",
          "[xsim][golden_ref_x]") {
  const auto fixtures = loadable_fixtures();
  REQUIRE(fixtures.size() > 30);
  GoldenRefSim two;
  GoldenRefSimX three;
  std::mt19937 rng(20261110);
  for (const std::string& name : fixtures) {
    INFO(name);
    const NormalizedGraph ng = test::load_normalized(name);
    const CompiledSimGraph cg = GraphCompiler::compile(ng);
    std::vector<CompactFault> faults = enumerate_faults(ng, cg);
    faults.push_back(CompactFault{});  // and one not active (fault-free)
    faults.back().exclusion = FaultExclusion::BLACKBOX;
    for (int seq = 0; seq < 6; ++seq) {
      TestVector vec;
      for (int c = 0; c < 7; ++c) {
        TestCycle tc;
        for (int pi : cg.pi_nets) {
          tc.inputs[cg.compiled_to_yosys[pi]] = (rng() & 1) != 0;
        }
        tc.sample_outputs = (rng() % 3) != 0;
        tc.fault_active = (rng() % 4) != 0;
        vec.cycles.push_back(tc);
      }
      for (uint32_t ff = 0; ff < cg.ff_configs.size(); ++ff) {
        if (rng() & 1) {
          vec.initial_ff_state[ff] = (rng() & 1) != 0;
        }
      }
      XRunOptions known;
      known.initial_state = L0;  // the two-valued default, so nothing is X
      for (const CompactFault& f : faults) {
        const auto want = two.simulate_sequence_with_fault(cg, vec, f);
        const auto got = three.simulate_sequence(cg, vec, &f, known);
        REQUIRE(got.size() == want.size());
        for (size_t s = 0; s < want.size(); ++s) {
          REQUIRE(got[s] == as_logic(want[s]));
        }
      }
    }
  }
}

TEST_CASE("A flop powers up X and is known once clocked", "[xsim][golden_ref_x]") {
  // tiny_dff: CLK=2, D=3, Q=4.
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  GoldenRefSimX sim;
  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, true}}), cycle({{2, true}, {3, true}})};
  const auto s = sim.simulate_sequence(cg, vec, nullptr, XRunOptions{});
  REQUIRE(s.size() == 2);
  REQUIRE(s[0].at(4) == LX);
  REQUIRE(s[1].at(4) == L1);

  // A known initial value overrides the power-up X, per flop.
  vec.initial_ff_state[0] = true;
  const auto k = sim.simulate_sequence(cg, vec, nullptr, XRunOptions{});
  REQUIRE(k[0].at(4) == L1);
}

TEST_CASE("An unknown clock keeps a flop's value only where D agrees with Q",
          "[xsim][golden_ref_x]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  GoldenRefSimX sim;
  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, true}}), cycle({{2, true}, {3, true}}),
                cycle({{2, false}, {3, true}}), cycle({{2, false}, {3, true}}),
                cycle({{2, false}, {3, false}}), cycle({{2, false}, {3, false}})};
  XRunOptions opt;
  opt.unknown_inputs = {{}, {}, {}, {2}, {}, {2}};  // CLK unknown in cycles 3 and 5
  const auto s = sim.simulate_sequence(cg, vec, nullptr, opt);
  REQUIRE(s.size() == 6);
  REQUIRE(s[1].at(4) == L1);
  REQUIRE(s[3].at(4) == L1);  // a possible edge with D == Q == 1: still 1
  REQUIRE(s[5].at(4) == LX);  // a possible edge with D = 0, Q = 1: unknown
}

TEST_CASE("Asynchronous clear and preset decide a flop whatever its state",
          "[xsim][golden_ref_x]") {
  // tiny_dffsr (dfbbp): CLK=2, D=3, RESET_B=4, SET_B=5, Q=6; both active low.
  const CompiledSimGraph cg = test::load_compiled("tiny_dffsr.json");
  GoldenRefSimX sim;
  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, false}, {4, true}, {5, true}}),
                cycle({{2, false}, {3, false}, {4, false}, {5, true}}),
                cycle({{2, false}, {3, false}, {4, true}, {5, true}}),
                cycle({{2, false}, {3, false}, {4, true}, {5, true}})};
  XRunOptions opt;
  opt.unknown_inputs = {{}, {}, {}, {4}};  // RESET_B unknown in cycle 3
  const auto s = sim.simulate_sequence(cg, vec, nullptr, opt);
  REQUIRE(s[0].at(6) == LX);  // powered up unknown
  REQUIRE(s[1].at(6) == L0);  // cleared
  REQUIRE(s[2].at(6) == L0);  // holds
  REQUIRE(s[3].at(6) == L0);  // an unknown clear on a flop already 0 leaves it 0
}

TEST_CASE("A data enable that is unknown keeps the flop only where D agrees with Q",
          "[xsim][golden_ref_x]") {
  // tiny_edfxtp: CLK=2, D=3, DE=4, Q=5.
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  GoldenRefSimX sim;
  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, true}, {4, true}}),
                cycle({{2, true}, {3, true}, {4, true}}),
                cycle({{2, false}, {3, true}, {4, true}}),
                cycle({{2, true}, {3, true}, {4, true}}),
                cycle({{2, false}, {3, false}, {4, true}}),
                cycle({{2, true}, {3, false}, {4, true}})};
  XRunOptions opt;
  opt.unknown_inputs = {{}, {}, {4}, {4}, {4}, {4}};  // DE unknown from cycle 2
  const auto s = sim.simulate_sequence(cg, vec, nullptr, opt);
  REQUIRE(s[1].at(5) == L1);
  REQUIRE(s[3].at(5) == L1);  // D == Q: known
  REQUIRE(s[5].at(5) == LX);  // D != Q: unknown
}

TEST_CASE("Detections split into definite and possible", "[xsim][golden_ref_x]") {
  // tiny_dff: CLK=2, D=3, Q=4, all flops X at power-up.
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  GoldenRefSimX sim;
  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, true}}), cycle({{2, true}, {3, true}})};
  const XRunOptions opt;
  const auto good = sim.simulate_sequence(cg, vec, nullptr, opt);

  // D stuck at 0: the flop loads 0 against the good machine's 1.
  const CompactFault d0 = fault_on(cg, 3, FaultType::SA0);
  REQUIRE(classify_detection(cg, good, sim.simulate_sequence(cg, vec, &d0, opt)) ==
          XDetection::DEFINITE);

  // The clock stuck at 0: the flop never loads, so it stays X where the good one is 1.
  const CompactFault clk0 = fault_on(cg, 2, FaultType::SA0);
  REQUIRE(classify_detection(cg, good, sim.simulate_sequence(cg, vec, &clk0, opt)) ==
          XDetection::POSSIBLE);

  // Q stuck at 1 differs only in cycle 0, where the good machine is X: no credit.
  TestVector first;
  first.cycles = {cycle({{2, false}, {3, true}})};
  const auto good0 = sim.simulate_sequence(cg, first, nullptr, opt);
  const CompactFault q1 = fault_on(cg, 4, FaultType::SA1);
  REQUIRE(classify_detection(cg, good0, sim.simulate_sequence(cg, first, &q1, opt)) ==
          XDetection::NONE);
}
