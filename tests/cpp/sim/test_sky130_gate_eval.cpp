#include <catch2/catch_test_macros.hpp>

#include <functional>
#include <map>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "sim/gate_eval.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

bool ref(GateType type, const std::vector<bool>& in) {
  return eval_gate_scalar(type, in);
}

void check_table(GateType type, int arity,
                 const std::function<bool(const std::vector<bool>&)>& expected) {
  for (int mask = 0; mask < (1 << arity); ++mask) {
    std::vector<bool> in;
    for (int i = 0; i < arity; ++i) {
      in.push_back((mask & (1 << i)) != 0);
    }
    REQUIRE(ref(type, in) == expected(in));
  }
}

}  // namespace

TEST_CASE("Sky130 primitive truth tables are independently pinned",
          "[sky130][gate_eval]") {
  check_table(GateType::MUX2_NI, 3, [](const auto& in) {
    return in[2] ? in[1] : in[0];
  });
  check_table(GateType::AND2B, 2, [](const auto& in) {
    return in[0] && !in[1];
  });
  check_table(GateType::A22O, 4, [](const auto& in) {
    return (in[0] && in[1]) || (in[2] && in[3]);
  });
  check_table(GateType::O21A, 3, [](const auto& in) {
    return (in[0] || in[1]) && in[2];
  });
  check_table(GateType::A2111OI, 5, [](const auto& in) {
    return !((in[0] && in[1]) || in[2] || in[3] || in[4]);
  });
  check_table(GateType::O311AI, 5, [](const auto& in) {
    return !((in[0] || in[1] || in[2]) && in[3] && in[4]);
  });
  check_table(GateType::NAND3B, 3, [](const auto& in) {
    return in[0] || !in[1] || !in[2];
  });
  check_table(GateType::NOR4B, 4, [](const auto& in) {
    return !in[0] && !in[1] && !in[2] && in[3];
  });
  check_table(GateType::OR4B, 4, [](const auto& in) {
    return in[0] || in[1] || in[2] || !in[3];
  });
  check_table(GateType::XNOR3, 3, [](const auto& in) {
    return !(in[0] ^ in[1] ^ in[2]);
  });
  check_table(GateType::A211OI, 4, [](const auto& in) {
    return !((in[0] && in[1]) || in[2] || in[3]);
  });
  check_table(GateType::O32AI, 5, [](const auto& in) {
    return !((in[0] | in[1] | in[2]) & (in[3] | in[4]));
  });
  check_table(GateType::NOR3B, 3, [](const auto& in) {
    return !in[0] && !in[1] && in[2];
  });
  check_table(GateType::A21OI, 3, [](const auto& in) {
    return !((in[0] && in[1]) || in[2]);
  });
  check_table(GateType::A22OI, 4, [](const auto& in) {
    return !((in[0] && in[1]) || (in[2] && in[3]));
  });
  check_table(GateType::O21AI, 3, [](const auto& in) {
    return !((in[0] || in[1]) && in[2]);
  });
  check_table(GateType::O211AI, 4, [](const auto& in) {
    return !((in[0] || in[1]) && in[2] && in[3]);
  });
  check_table(GateType::O31AI, 4, [](const auto& in) {
    return !((in[0] || in[1] || in[2]) && in[3]);
  });
  check_table(GateType::NOR2B, 2, [](const auto& in) {
    return !in[0] && in[1];
  });
  check_table(GateType::OR3B, 3, [](const auto& in) {
    return in[0] || in[1] || !in[2];
  });
  // DSP/crypto cell additions.
  check_table(GateType::OR2B, 2, [](const auto& in) {
    return in[0] || !in[1];
  });
  check_table(GateType::OR4BB, 4, [](const auto& in) {
    return in[0] || in[1] || !in[2] || !in[3];
  });
  check_table(GateType::AND4BB, 4, [](const auto& in) {
    return !in[0] && !in[1] && in[2] && in[3];
  });
  check_table(GateType::A2BB2O, 4, [](const auto& in) {
    return (!in[0] && !in[1]) || (in[2] && in[3]);
  });
  // nand4bb: Y = A_N | B_N | !C | !D (first two inputs pre-inverted/bubbled).
  check_table(GateType::NAND4BB, 4, [](const auto& in) {
    return in[0] || in[1] || !in[2] || !in[3];
  });
}

// Regression for the NAND4BB pin-name bug: the cell map + graph compiler once
// wired the nonexistent pins A1_N/A2_N/B1/B2, so all four inputs silently
// became UNUSED and the gate evaluated as a constant 1 regardless of inputs.
// This exercises the full parse->normalize->compile->simulate path against the
// independent truth table, so it fails on the broken wiring and passes on the
// real ports A_N/B_N/C/D.
TEST_CASE("nand4bb wires its real ports (A_N/B_N/C/D), not a constant",
          "[sky130][nand4bb][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_nand4bb.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 16; ++mask) {
    const bool a_n = (mask & 1) != 0;
    const bool b_n = (mask & 2) != 0;
    const bool c = (mask & 4) != 0;
    const bool d = (mask & 8) != 0;
    TestVector vec;
    vec.inputs = {{2, a_n}, {3, b_n}, {4, c}, {5, d}};  // A_N,B_N,C,D net ids
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(6) == (a_n || b_n || !c || !d));  // Y net id 6
  }
}
