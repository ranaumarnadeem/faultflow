#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/gate_eval.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

// tiny_icg.json: CLK 2, EN 3, TE 4, D 5 -> sdlclkp (GCLK 7) -> dfxtp Q 6.
constexpr int kClk = 2, kEn = 3, kTe = 4, kD = 5, kQ = 6, kGclk = 7;

TestCycle at(bool clk, bool en, bool te, bool d) {
  TestCycle c;
  c.inputs = {{kClk, clk}, {kEn, en}, {kTe, te}, {kD, d}};
  c.sample_outputs = true;
  return c;
}

std::vector<bool> q_trace(const CompiledSimGraph& cg, const TestVector& vec) {
  GoldenRefSim sim;
  std::vector<bool> q;
  for (const auto& sample : sim.simulate_sequence_fault_free(cg, vec)) {
    q.push_back(sample.at(kQ));
  }
  return q;
}

}  // namespace

TEST_CASE("A clock gate normalizes to a stateful gate on clock nets", "[icg]") {
  const NormalizedGraph ng = test::load_normalized("tiny_icg.json");
  const auto it = std::find_if(ng.nodes.begin(), ng.nodes.end(), [](const auto& n) {
    return n.second.instance == "u_icg";
  });
  REQUIRE(it != ng.nodes.end());
  REQUIRE(it->second.type == NodeType::GATE);
  REQUIRE(it->second.gate_type == GateType::ICG_POS);
  REQUIRE(it->second.input_order == std::vector<std::string>{"CLK", "GATE", "SCE"});
  REQUIRE(ng.nets.at(kClk).is_clock);
  REQUIRE(ng.nets.at(kGclk).is_clock);
  REQUIRE_FALSE(ng.nets.at(kEn).is_clock);
  REQUIRE_FALSE(ng.nets.at(kTe).is_clock);
}

TEST_CASE("A clock gate passes a clock edge only while its latched enable is set",
          "[icg]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_icg.json");
  TestVector vec;
  vec.cycles = {
      at(false, true, false, true),   // 0: enabled, D 1
      at(true, true, false, true),    // 1: edge -> Q 1
      at(false, false, false, false), // 2: disabled, D 0
      at(true, false, false, false),  // 3: no edge: Q holds 1
      at(false, true, false, false),  // 4: enabled again
      at(true, false, false, false),  // 5: EN falls with the edge: latched 1, Q 0
      at(true, true, false, true),    // 6: EN rises while CLK is high: no new edge
      at(false, false, false, true),  // 7: disabled
      at(true, true, false, true),    // 8: EN rises with the edge: latched 0, Q holds
      at(false, false, true, true),   // 9: test enable only
      at(true, false, true, true),    // 10: edge -> Q 1
  };
  const std::vector<bool> want = {false, true, true, true, true, false,
                                  false, false, false, false, true};
  REQUIRE(q_trace(cg, vec) == want);
}

TEST_CASE("Bit-parallel and golden agree on a clock gate's enable faults", "[icg]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_icg.json");
  TestVector vec;
  vec.cycles = {
      at(false, true, false, true),
      at(true, true, false, true),
      at(false, false, false, false),
      at(true, false, false, false),
      at(false, false, true, false),
      at(true, false, true, false),
  };
  GoldenRefSim golden;
  BitParallelSim parallel;
  const auto good = golden.simulate_sequence_fault_free(cg, vec);
  for (const int net : {kEn, kTe}) {
    for (const FaultType type : {FaultType::SA0, FaultType::SA1}) {
      CompactFault fault;
      fault.net_index = cg.yosys_to_compiled.at(net);
      fault.type = type;
      const auto bad = golden.simulate_sequence_with_fault(cg, vec, fault);
      const bool detected = golden.is_sequence_detected(cg, good, bad);
      INFO("net " << net << " sa" << (type == FaultType::SA1 ? 1 : 0));
      REQUIRE(parallel.simulate_single_fault(cg, vec, fault) == detected);
      // EN or TE stuck-at-1 opens the gate for cycle 3 (Q captures 0); EN
      // stuck-at-0 misses cycle 1's edge, TE stuck-at-0 cycle 5's.
      REQUIRE(detected);
    }
  }
}

TEST_CASE("A gated clock that fans out reaches every flop on the same edge",
          "[icg]") {
  // tiny_icg_fanout.json: CLK 2, EN 3, D 5 -> dlclkp (GCLK 7) -> two flops in a
  // row, Q0 6 -> Q1 8. Each reads GCLK through its own branch, which must be
  // settled after the gate before the flops update.
  const CompiledSimGraph cg = test::load_compiled("tiny_icg_fanout.json");
  GoldenRefSim golden;
  TestVector vec;
  for (const bool d : {true, false, true}) {
    for (const bool clk : {false, true}) {
      TestCycle c;
      c.inputs = {{2, clk}, {3, true}, {5, d}};
      c.sample_outputs = true;
      vec.cycles.push_back(c);
    }
  }
  std::vector<std::pair<bool, bool>> got;
  for (const auto& s : golden.simulate_sequence_fault_free(cg, vec)) {
    got.emplace_back(s.at(6), s.at(8));
  }
  const std::vector<std::pair<bool, bool>> want = {
      {false, false}, {true, false}, {true, false},
      {false, true},  {false, true}, {true, false}};
  REQUIRE(got == want);
  // The bit-parallel engine sees the same edges: Q1's stuck-at-0 shows.
  CompactFault q1_sa0;
  q1_sa0.net_index = cg.yosys_to_compiled.at(8);
  q1_sa0.type = FaultType::SA0;
  BitParallelSim parallel;
  REQUIRE(parallel.simulate_single_fault(cg, vec, q1_sa0));
}

TEST_CASE("In a scan ATPG view a clock gate is its enable", "[icg]") {
  // tiny_icg_view.json: tiny_icg with u_icg marked faultflow_icg_enable=root.
  const NormalizedGraph ng = test::load_normalized("tiny_icg_view.json");
  const auto it = std::find_if(ng.nodes.begin(), ng.nodes.end(), [](const auto& n) {
    return n.second.instance == "u_icg";
  });
  REQUIRE(it != ng.nodes.end());
  REQUIRE(it->second.gate_type == GateType::ICG_EN);
  const CompiledSimGraph cg = test::load_compiled("tiny_icg_view.json");
  GoldenRefSim golden;
  for (const bool en : {false, true}) {
    for (const bool te : {false, true}) {
      TestVector vec;
      vec.inputs = {{kClk, false}, {kEn, en}, {kTe, te}, {kD, false}};
      REQUIRE(golden.simulate_fault_free(cg, vec).at(kGclk) == (en || te));
    }
  }
}

TEST_CASE("A falling-edge clock gate holds its clock high while disabled",
          "[icg]") {
  // GF180MCU icgtn: GCLK = CLKN | ~L, L loaded while CLKN is high.
  uint64_t latch = 0;
  // CLKN high, enabled: L loads 1, GCLK high.
  REQUIRE(eval_icg(GateType::ICG_NEG, ~0ULL, ~0ULL, latch) == ~0ULL);
  // CLKN falls with EN dropping: L holds 1, the falling edge passes.
  REQUIRE(eval_icg(GateType::ICG_NEG, 0ULL, 0ULL, latch) == 0ULL);
  // CLKN high, disabled: L loads 0; then CLKN falls: GCLK stays high.
  REQUIRE(eval_icg(GateType::ICG_NEG, ~0ULL, 0ULL, latch) == ~0ULL);
  REQUIRE(eval_icg(GateType::ICG_NEG, 0ULL, ~0ULL, latch) == ~0ULL);
}
