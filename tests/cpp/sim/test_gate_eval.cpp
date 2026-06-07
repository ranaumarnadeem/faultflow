#include <catch2/catch_test_macros.hpp>

#include "common/errors.hpp"
#include "sim/gate_eval.hpp"

using namespace faultflow;

TEST_CASE("Gate evaluation basic gates", "[gate_eval]") {
  const uint64_t ALL1 = ~0ULL;
  const uint64_t ALL0 = 0ULL;
  const uint64_t ALT = 0xAAAAAAAAAAAAAAAAULL;
  const uint64_t HALT = 0x5555555555555555ULL;

  REQUIRE(eval_gate(GateType::INV, {ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::INV, {ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::INV, {ALT}) == HALT);

  REQUIRE(eval_gate(GateType::BUF, {ALT}) == ALT);

  REQUIRE(eval_gate(GateType::AND2, {ALL0, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::AND2, {ALL0, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::AND2, {ALL1, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::AND2, {ALT, HALT}) == ALL0);
  REQUIRE(eval_gate(GateType::AND2, {ALT, ALL1}) == ALT);

  REQUIRE(eval_gate(GateType::OR2, {ALL0, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::OR2, {ALL0, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::OR2, {ALL1, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::OR2, {ALT, HALT}) == ALL1);

  REQUIRE(eval_gate(GateType::NAND2, {ALL1, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::NAND2, {ALL0, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::NAND2, {ALT, HALT}) ==
          eval_gate(GateType::INV, {eval_gate(GateType::AND2, {ALT, HALT})}));
  REQUIRE(eval_gate(GateType::NAND3, {ALL1, ALL1, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::NAND3, {ALL1, ALL1, ALL0}) == ALL1);

  REQUIRE(eval_gate(GateType::NOR2, {ALL0, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::NOR2, {ALL1, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::NOR3, {ALL0, ALL0, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::NOR3, {ALL0, ALL0, ALL1}) == ALL0);

  REQUIRE(eval_gate(GateType::XOR2, {ALL0, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::XOR2, {ALL1, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::XOR2, {ALL1, ALL1}) == ALL0);

  REQUIRE(eval_gate(GateType::XNOR2, {ALL1, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::XNOR2, {ALL1, ALL0}) == ALL0);

  // OSU035 inverting MUX2: S=1 selects A (in0)
  REQUIRE(eval_gate(GateType::MUX2, {ALL1, ALL0, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::MUX2, {ALL0, ALL1, ALL0}) == ALL0);

  REQUIRE(eval_gate(GateType::OAI21, {ALL0, ALL0, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::OAI21, {ALL1, ALL0, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::AOI21, {ALL1, ALL1, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::AOI21, {ALL0, ALL1, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::OAI22, {ALL1, ALL0, ALL1, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::OAI22, {ALL0, ALL0, ALL1, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::AOI22, {ALL1, ALL1, ALL0, ALL1}) == ALL0);
  REQUIRE(eval_gate(GateType::AOI22, {ALL1, ALL0, ALL1, ALL1}) == ALL0);

  REQUIRE(eval_gate(GateType::CONST0, {}) == ALL0);
  REQUIRE(eval_gate(GateType::CONST1, {}) == ALL1);
  REQUIRE(eval_gate(GateType::ADDF_S, {ALL1, ALL1, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::ADDF_S, {ALL1, ALL1, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::ADDF_CO, {ALL1, ALL1, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::ADDF_CO, {ALL1, ALL0, ALL0}) == ALL0);
  REQUIRE(eval_gate(GateType::ADDH_S, {ALL1, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::ADDH_CO, {ALL1, ALL1}) == ALL1);
  REQUIRE(eval_gate(GateType::ADDH_CO, {ALL1, ALL0}) == ALL0);

  REQUIRE(eval_gate_scalar(GateType::OAI22, {true, false, true, false}) ==
          false);
  REQUIRE_THROWS_AS(eval_gate(static_cast<GateType>(255), {}), ParseError);
}
