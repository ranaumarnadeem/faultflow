#include <catch2/catch_test_macros.hpp>

#include <functional>
#include <vector>

#include "sim/gate_eval.hpp"

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
}
