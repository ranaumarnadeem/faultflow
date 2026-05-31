#include <catch2/catch_test_macros.hpp>

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

  REQUIRE(eval_gate(GateType::NOR2, {ALL0, ALL0}) == ALL1);
  REQUIRE(eval_gate(GateType::NOR2, {ALL1, ALL0}) == ALL0);
}
