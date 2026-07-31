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

// Regression for A311OI/MUX4/OR2B/OR4BB/AND4BB/A2BB2O: GraphCompiler::wire_inputs
// had no case for any of these six gate types (gate_eval.cpp evaluates them
// correctly, and they are present in the cell map, but wire_inputs fell through
// to `default: break;`), so every input stayed UNUSED_INPUT and each cell
// silently evaluated as a fixed constant regardless of its real inputs. Each
// case here exercises the full parse->normalize->compile->simulate pipeline
// against the same independent truth table already pinned above, so it fails
// on a constant output and passes once the real ports are wired.

TEST_CASE("a311oi wires its real ports (A1/A2/A3/B1/C1), not a constant",
          "[sky130][a311oi][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_a311oi.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 32; ++mask) {
    const bool a1 = (mask & 1) != 0;
    const bool a2 = (mask & 2) != 0;
    const bool a3 = (mask & 4) != 0;
    const bool b1 = (mask & 8) != 0;
    const bool c1 = (mask & 16) != 0;
    TestVector vec;
    vec.inputs = {{2, a1}, {3, a2}, {4, a3}, {5, b1}, {6, c1}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(7) == !((a1 && a2 && a3) || b1 || c1));  // Y net id 7
  }
}

TEST_CASE("mux4 wires its real ports (A0-A3/S0/S1), not a constant",
          "[sky130][mux4][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_mux4_gate.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 64; ++mask) {
    const bool a0 = (mask & 1) != 0;
    const bool a1 = (mask & 2) != 0;
    const bool a2 = (mask & 4) != 0;
    const bool a3 = (mask & 8) != 0;
    const bool s0 = (mask & 16) != 0;
    const bool s1 = (mask & 32) != 0;
    TestVector vec;
    vec.inputs = {{2, a0}, {3, a1}, {4, a2}, {5, a3}, {6, s0}, {7, s1}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    const bool expected = s1 ? (s0 ? a3 : a2) : (s0 ? a1 : a0);
    REQUIRE(vals.at(8) == expected);  // X net id 8
  }
}

TEST_CASE("or2b wires its real ports (A/B_N), not a constant",
          "[sky130][or2b][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_or2b.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 4; ++mask) {
    const bool a = (mask & 1) != 0;
    const bool b_n = (mask & 2) != 0;
    TestVector vec;
    vec.inputs = {{2, a}, {3, b_n}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(4) == (a || !b_n));  // X net id 4
  }
}

TEST_CASE("or4bb wires its real ports (A/B/C_N/D_N), not a constant",
          "[sky130][or4bb][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_or4bb.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 16; ++mask) {
    const bool a = (mask & 1) != 0;
    const bool b = (mask & 2) != 0;
    const bool c_n = (mask & 4) != 0;
    const bool d_n = (mask & 8) != 0;
    TestVector vec;
    vec.inputs = {{2, a}, {3, b}, {4, c_n}, {5, d_n}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(6) == (a || b || !c_n || !d_n));  // X net id 6
  }
}

TEST_CASE("and4bb wires its real ports (A_N/B_N/C/D), not a constant",
          "[sky130][and4bb][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_and4bb.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 16; ++mask) {
    const bool a_n = (mask & 1) != 0;
    const bool b_n = (mask & 2) != 0;
    const bool c = (mask & 4) != 0;
    const bool d = (mask & 8) != 0;
    TestVector vec;
    vec.inputs = {{2, a_n}, {3, b_n}, {4, c}, {5, d}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(6) == (!a_n && !b_n && c && d));  // X net id 6
  }
}

TEST_CASE("a2bb2o wires its real ports (A1_N/A2_N/B1/B2), not a constant",
          "[sky130][a2bb2o][compiled]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_a2bb2o.json");
  const GoldenRefSim ref;
  for (int mask = 0; mask < 16; ++mask) {
    const bool a1_n = (mask & 1) != 0;
    const bool a2_n = (mask & 2) != 0;
    const bool b1 = (mask & 4) != 0;
    const bool b2 = (mask & 8) != 0;
    TestVector vec;
    vec.inputs = {{2, a1_n}, {3, a2_n}, {4, b1}, {5, b2}};
    const std::map<int, bool> vals = ref.simulate_fault_free(cg, vec);
    REQUIRE(vals.at(6) == ((!a1_n && !a2_n) || (b1 && b2)));  // X net id 6
  }
}
