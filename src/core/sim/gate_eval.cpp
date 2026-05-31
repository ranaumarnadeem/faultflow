#include "sim/gate_eval.hpp"

namespace faultflow {
namespace {

uint64_t pick(const std::vector<uint64_t>& inputs, size_t i) {
  return i < inputs.size() ? inputs[i] : 0ULL;
}

bool pick_b(const std::vector<bool>& inputs, size_t i) {
  return i < inputs.size() && inputs[i];
}

uint64_t eval_bitwise(GateType type, const std::vector<uint64_t>& inputs) {
  const uint64_t in0 = pick(inputs, 0);
  const uint64_t in1 = pick(inputs, 1);
  const uint64_t in2 = pick(inputs, 2);
  const uint64_t in3 = pick(inputs, 3);

  switch (type) {
    case GateType::INV:
      return ~in0;
    case GateType::BUF:
    case GateType::INPUT:
      return in0;
    case GateType::AND2:
      return in0 & in1;
    case GateType::OR2:
      return in0 | in1;
    case GateType::NAND2:
      return ~(in0 & in1);
    case GateType::NAND3:
      return ~(in0 & in1 & in2);
    case GateType::NOR2:
      return ~(in0 | in1);
    case GateType::NOR3:
      return ~(in0 | in1 | in2);
    case GateType::XOR2:
      return in0 ^ in1;
    case GateType::XNOR2:
      return ~(in0 ^ in1);
    case GateType::OAI21:
      return ~((in0 | in1) & in2);
    case GateType::AOI21:
      return ~((in0 & in1) | in2);
    case GateType::OAI22:
      return ~((in0 | in1) & (in2 | in3));
    case GateType::AOI22:
      return ~((in0 & in1) | (in2 & in3));
    // OSU035 MUX2X1: Y = ~((S&A)|(~S&B)); pins A,B,S -> in0,in1,in2
    case GateType::MUX2:
      return ~((in2 & in0) | (~in2 & in1));
    case GateType::CONST0:
      return 0ULL;
    case GateType::CONST1:
      return ~0ULL;
    case GateType::ADDF_S:
      return in0 ^ in1 ^ in2;
    case GateType::ADDF_CO:
      return (in0 & in1) | (in1 & in2) | (in0 & in2);
    case GateType::ADDH_S:
      return in0 ^ in1;
    case GateType::ADDH_CO:
      return in0 & in1;
    default:
      return 0ULL;
  }
}

bool eval_scalar(GateType type, const std::vector<bool>& inputs) {
  const uint64_t w = eval_bitwise(type, {pick_b(inputs, 0) ? ~0ULL : 0ULL,
                                         pick_b(inputs, 1) ? ~0ULL : 0ULL,
                                         pick_b(inputs, 2) ? ~0ULL : 0ULL,
                                         pick_b(inputs, 3) ? ~0ULL : 0ULL});
  return (w & 1ULL) != 0;
}

}  // namespace

uint64_t eval_gate(GateType type, const std::vector<uint64_t>& inputs) {
  return eval_bitwise(type, inputs);
}

bool eval_gate_scalar(GateType type, const std::vector<bool>& inputs) {
  return eval_scalar(type, inputs);
}

}  // namespace faultflow
