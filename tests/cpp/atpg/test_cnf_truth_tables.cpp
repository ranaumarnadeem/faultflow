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
