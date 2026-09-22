#include <catch2/catch_test_macros.hpp>

#include <cadical.hpp>
#include <functional>
#include <vector>

#include "atpg/cnf_encoder.hpp"
#include "common/types.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

namespace {

void check_cnf_table(GateType type, int arity,
                     const std::function<bool(const std::vector<bool>&)>& expected) {
  for (int mask = 0; mask < (1 << arity); ++mask) {
    std::vector<bool> bits;
    bits.reserve(arity);
    for (int i = 0; i < arity; ++i) {
      bits.push_back((mask & (1 << i)) != 0);
    }

    CaDiCaL::Solver solver;
    std::vector<int> inputs;
    inputs.reserve(arity);
    for (int i = 0; i < arity; ++i) {
      inputs.push_back(i + 1);
    }
    const int output = arity + 1;
    add_gate_cnf(solver, type, inputs, output);

    for (int i = 0; i < arity; ++i) {
      add_unit(solver, bits[i] ? inputs[i] : -inputs[i]);
    }

    const int result = solver.solve();
    REQUIRE(result == 10);
    const bool actual = solver.val(output) > 0;
    REQUIRE(actual == expected(bits));
  }
}

}  // namespace

TEST_CASE("CNF truth tables wave 1 primitives", "[atpg][cnf]") {
  check_cnf_table(GateType::AND2, 2, [](const auto& in) { return in[0] && in[1]; });
  check_cnf_table(GateType::OR2, 2, [](const auto& in) { return in[0] || in[1]; });
  check_cnf_table(GateType::XOR2, 2, [](const auto& in) { return in[0] ^ in[1]; });
}

TEST_CASE("CNF truth tables wave 2 primitives", "[atpg][cnf]") {
  check_cnf_table(GateType::INV, 1, [](const auto& in) { return !in[0]; });
  check_cnf_table(GateType::BUF, 1, [](const auto& in) { return in[0]; });
  check_cnf_table(GateType::NAND2, 2,
                  [](const auto& in) { return !(in[0] && in[1]); });
  check_cnf_table(GateType::NOR2, 2, [](const auto& in) { return !(in[0] || in[1]); });
  check_cnf_table(GateType::XNOR2, 2,
                  [](const auto& in) { return !(in[0] ^ in[1]); });
}

TEST_CASE("CNF truth tables wave 3 Sky130 lowered primitives", "[atpg][cnf][sky130]") {
  check_cnf_table(GateType::MUX2_NI, 3, [](const auto& in) {
    return in[2] ? in[1] : in[0];
  });
  check_cnf_table(GateType::AND2B, 2, [](const auto& in) {
    return in[0] && !in[1];
  });
  check_cnf_table(GateType::A22O, 4, [](const auto& in) {
    return (in[0] && in[1]) || (in[2] && in[3]);
  });
  check_cnf_table(GateType::O21A, 3, [](const auto& in) {
    return (in[0] || in[1]) && in[2];
  });
  check_cnf_table(GateType::A2111OI, 5, [](const auto& in) {
    return !((in[0] && in[1]) || in[2] || in[3] || in[4]);
  });
  check_cnf_table(GateType::O311AI, 5, [](const auto& in) {
    return !((in[0] || in[1] || in[2]) && in[3] && in[4]);
  });
  check_cnf_table(GateType::NAND3B, 3, [](const auto& in) {
    return in[0] || !in[1] || !in[2];
  });
  check_cnf_table(GateType::NOR4B, 4, [](const auto& in) {
    return !in[0] && !in[1] && !in[2] && in[3];
  });
  check_cnf_table(GateType::OR4B, 4, [](const auto& in) {
    return in[0] || in[1] || in[2] || !in[3];
  });
  check_cnf_table(GateType::XNOR3, 3, [](const auto& in) {
    return !(in[0] ^ in[1] ^ in[2]);
  });
  check_cnf_table(GateType::A211OI, 4, [](const auto& in) {
    return !((in[0] && in[1]) || in[2] || in[3]);
  });
  check_cnf_table(GateType::O32AI, 5, [](const auto& in) {
    return !((in[0] | in[1] | in[2]) & (in[3] | in[4]));
  });
  check_cnf_table(GateType::NOR3B, 3, [](const auto& in) {
    return !in[0] && !in[1] && in[2];
  });
  check_cnf_table(GateType::A21OI, 3, [](const auto& in) {
    return !((in[0] && in[1]) || in[2]);
  });
  check_cnf_table(GateType::A22OI, 4, [](const auto& in) {
    return !((in[0] && in[1]) || (in[2] && in[3]));
  });
  check_cnf_table(GateType::O21AI, 3, [](const auto& in) {
    return !((in[0] || in[1]) && in[2]);
  });
  check_cnf_table(GateType::O211AI, 4, [](const auto& in) {
    return !((in[0] || in[1]) && in[2] && in[3]);
  });
  check_cnf_table(GateType::O31AI, 4, [](const auto& in) {
    return !((in[0] || in[1] || in[2]) && in[3]);
  });
  check_cnf_table(GateType::NOR2B, 2, [](const auto& in) {
    return !in[0] && in[1];
  });
  check_cnf_table(GateType::OR3B, 3, [](const auto& in) {
    return in[0] || in[1] || !in[2];
  });
}

// Waves 4-7 below: each formula was cross-checked against the real Sky130
// (or, where a gate has no Sky130 mapping, OSU035) Liberty file's own
// `pin (Y) { function : "..." }` string via tools/derive_gate_truth_tables.py
// -- a source independent of gate_eval.cpp, since it comes from the PDK
// vendor's own timing library, not from this codebase. All 63 gates matched
// gate_eval.cpp exactly; see that script for the derivation/cross-check.

TEST_CASE("CNF truth tables wave 4 basic n-input and bubbled primitives",
          "[atpg][cnf][sky130]") {
  check_cnf_table(GateType::AND3, 3,
                  [](const auto& in) { return in[0] && in[1] && in[2]; });
  check_cnf_table(GateType::AND3B, 3, [](const auto& in) {
    return !in[0] && in[1] && in[2];
  });
  check_cnf_table(GateType::AND4, 4, [](const auto& in) {
    return in[0] && in[1] && in[2] && in[3];
  });
  check_cnf_table(GateType::AND4B, 4, [](const auto& in) {
    return !in[0] && in[1] && in[2] && in[3];
  });
  check_cnf_table(GateType::AND4BB, 4, [](const auto& in) {
    return !in[0] && !in[1] && in[2] && in[3];
  });
  check_cnf_table(GateType::OR3, 3,
                  [](const auto& in) { return in[0] || in[1] || in[2]; });
  check_cnf_table(GateType::OR4, 4, [](const auto& in) {
    return in[0] || in[1] || in[2] || in[3];
  });
  check_cnf_table(GateType::OR2B, 2,
                  [](const auto& in) { return in[0] || !in[1]; });
  check_cnf_table(GateType::OR4BB, 4, [](const auto& in) {
    return in[0] || in[1] || !in[2] || !in[3];
  });
  check_cnf_table(GateType::NAND2B, 2,
                  [](const auto& in) { return in[0] || !in[1]; });
  check_cnf_table(GateType::NAND3, 3, [](const auto& in) {
    return !(in[0] && in[1] && in[2]);
  });
  check_cnf_table(GateType::NAND4, 4, [](const auto& in) {
    return !(in[0] && in[1] && in[2] && in[3]);
  });
  check_cnf_table(GateType::NAND4B, 4, [](const auto& in) {
    return in[0] || !in[1] || !in[2] || !in[3];
  });
  check_cnf_table(GateType::NAND4BB, 4, [](const auto& in) {
    return in[0] || in[1] || !in[2] || !in[3];
  });
  check_cnf_table(GateType::NOR3, 3, [](const auto& in) {
    return !(in[0] || in[1] || in[2]);
  });
  check_cnf_table(GateType::NOR4, 4, [](const auto& in) {
    return !(in[0] || in[1] || in[2] || in[3]);
  });
  check_cnf_table(GateType::NOR4BB, 4, [](const auto& in) {
    return !in[0] && !in[1] && in[2] && in[3];
  });
  check_cnf_table(GateType::XOR3, 3,
                  [](const auto& in) { return in[0] ^ in[1] ^ in[2]; });
}

TEST_CASE("CNF truth tables wave 5 2-4 input AOI/OAI and AND-OR compounds",
          "[atpg][cnf][sky130]") {
  check_cnf_table(GateType::AOI21, 3, [](const auto& in) {
    return !((in[0] && in[1]) || in[2]);
  });
  check_cnf_table(GateType::AOI22, 4, [](const auto& in) {
    return !((in[0] && in[1]) || (in[2] && in[3]));
  });
  check_cnf_table(GateType::OAI21, 3, [](const auto& in) {
    return !((in[0] || in[1]) && in[2]);
  });
  check_cnf_table(GateType::OAI22, 4, [](const auto& in) {
    return !((in[0] || in[1]) && (in[2] || in[3]));
  });
  check_cnf_table(GateType::A21O, 3, [](const auto& in) {
    return (in[0] && in[1]) || in[2];
  });
  check_cnf_table(GateType::A21BO, 3, [](const auto& in) {
    return (in[0] && in[1]) || !in[2];
  });
  check_cnf_table(GateType::A21BOI, 3, [](const auto& in) {
    return !((in[0] && in[1]) || !in[2]);
  });
  check_cnf_table(GateType::O21BA, 3, [](const auto& in) {
    return (in[0] || in[1]) && !in[2];
  });
  check_cnf_table(GateType::O21BAI, 3, [](const auto& in) {
    return !((in[0] || in[1]) && !in[2]);
  });
  check_cnf_table(GateType::O22A, 4, [](const auto& in) {
    return (in[0] || in[1]) && (in[2] || in[3]);
  });
  check_cnf_table(GateType::O22AI, 4, [](const auto& in) {
    return !((in[0] || in[1]) && (in[2] || in[3]));
  });
  check_cnf_table(GateType::A2BB2O, 4, [](const auto& in) {
    return (!in[0] && !in[1]) || (in[2] && in[3]);
  });
  check_cnf_table(GateType::A2BB2OI, 4, [](const auto& in) {
    return (in[0] || in[1]) && !(in[2] && in[3]);
  });
  check_cnf_table(GateType::O2BB2A, 4, [](const auto& in) {
    return !(in[0] && in[1]) && (in[2] || in[3]);
  });
  check_cnf_table(GateType::O2BB2AI, 4, [](const auto& in) {
    return (in[0] && in[1]) || !(in[2] || in[3]);
  });
  check_cnf_table(GateType::A211O, 4, [](const auto& in) {
    return (in[0] && in[1]) || in[2] || in[3];
  });
}

TEST_CASE("CNF truth tables wave 6 5-6 input AND-OR/OR-AND compounds",
          "[atpg][cnf][sky130]") {
  check_cnf_table(GateType::A2111O, 5, [](const auto& in) {
    return (in[0] && in[1]) || in[2] || in[3] || in[4];
  });
  check_cnf_table(GateType::A221O, 5, [](const auto& in) {
    return (in[0] && in[1]) || (in[2] && in[3]) || in[4];
  });
  check_cnf_table(GateType::A221OI, 5, [](const auto& in) {
    return !((in[0] && in[1]) || (in[2] && in[3]) || in[4]);
  });
  check_cnf_table(GateType::A222OI, 6, [](const auto& in) {
    return !((in[0] && in[1]) || (in[2] && in[3]) || (in[4] && in[5]));
  });
  check_cnf_table(GateType::A311O, 5, [](const auto& in) {
    return (in[0] && in[1] && in[2]) || in[3] || in[4];
  });
  check_cnf_table(GateType::A311OI, 5, [](const auto& in) {
    return !((in[0] && in[1] && in[2]) || in[3] || in[4]);
  });
  check_cnf_table(GateType::A31O, 4, [](const auto& in) {
    return (in[0] && in[1] && in[2]) || in[3];
  });
  check_cnf_table(GateType::A31OI, 4, [](const auto& in) {
    return !((in[0] && in[1] && in[2]) || in[3]);
  });
  check_cnf_table(GateType::A32O, 5, [](const auto& in) {
    return (in[0] && in[1] && in[2]) || (in[3] && in[4]);
  });
  check_cnf_table(GateType::A32OI, 5, [](const auto& in) {
    return !((in[0] && in[1] && in[2]) || (in[3] && in[4]));
  });
  check_cnf_table(GateType::A41O, 5, [](const auto& in) {
    return (in[0] && in[1] && in[2] && in[3]) || in[4];
  });
  check_cnf_table(GateType::A41OI, 5, [](const auto& in) {
    return !((in[0] && in[1] && in[2] && in[3]) || in[4]);
  });
  check_cnf_table(GateType::O2111A, 5, [](const auto& in) {
    return (in[0] || in[1]) && in[2] && in[3] && in[4];
  });
  check_cnf_table(GateType::O2111AI, 5, [](const auto& in) {
    return !((in[0] || in[1]) && in[2] && in[3] && in[4]);
  });
  check_cnf_table(GateType::O211A, 4, [](const auto& in) {
    return (in[0] || in[1]) && in[2] && in[3];
  });
  check_cnf_table(GateType::O221A, 5, [](const auto& in) {
    return (in[0] || in[1]) && (in[2] || in[3]) && in[4];
  });
  check_cnf_table(GateType::O221AI, 5, [](const auto& in) {
    return !((in[0] || in[1]) && (in[2] || in[3]) && in[4]);
  });
  check_cnf_table(GateType::O311A, 5, [](const auto& in) {
    return (in[0] || in[1] || in[2]) && in[3] && in[4];
  });
  check_cnf_table(GateType::O31A, 4, [](const auto& in) {
    return (in[0] || in[1] || in[2]) && in[3];
  });
  check_cnf_table(GateType::O32A, 5, [](const auto& in) {
    return (in[0] || in[1] || in[2]) && (in[3] || in[4]);
  });
  check_cnf_table(GateType::O41A, 5, [](const auto& in) {
    return (in[0] || in[1] || in[2] || in[3]) && in[4];
  });
  check_cnf_table(GateType::O41AI, 5, [](const auto& in) {
    return !((in[0] || in[1] || in[2] || in[3]) && in[4]);
  });
}

TEST_CASE("CNF truth tables wave 7 mux and adder primitives",
          "[atpg][cnf][sky130]") {
  check_cnf_table(GateType::MUX2, 3, [](const auto& in) {
    return !((in[2] && in[0]) || (!in[2] && in[1]));
  });
  check_cnf_table(GateType::MUX2I, 3, [](const auto& in) {
    return !((!in[2] && in[0]) || (in[2] && in[1]));
  });
  check_cnf_table(GateType::MUX4, 6, [](const auto& in) {
    return (!in[4] && !in[5] && in[0]) || (in[4] && !in[5] && in[1]) ||
           (!in[4] && in[5] && in[2]) || (in[4] && in[5] && in[3]);
  });
  check_cnf_table(GateType::ADDF_S, 3, [](const auto& in) {
    return in[0] ^ in[1] ^ in[2];
  });
  check_cnf_table(GateType::ADDF_CO, 3, [](const auto& in) {
    return (in[0] && in[1]) || (in[1] && in[2]) || (in[0] && in[2]);
  });
  check_cnf_table(GateType::ADDH_S, 2,
                  [](const auto& in) { return in[0] ^ in[1]; });
  check_cnf_table(GateType::ADDH_CO, 2,
                  [](const auto& in) { return in[0] && in[1]; });
}
