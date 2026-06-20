#include "sim/gate_eval.hpp"

#include "common/errors.hpp"

namespace faultflow {
namespace {

uint64_t pick(const std::vector<uint64_t>& inputs, size_t i) {
  return i < inputs.size() ? inputs[i] : 0ULL;
}

uint64_t eval_bitwise(GateType type, const std::vector<uint64_t>& inputs) {
  const uint64_t in0 = pick(inputs, 0);
  const uint64_t in1 = pick(inputs, 1);
  const uint64_t in2 = pick(inputs, 2);
  const uint64_t in3 = pick(inputs, 3);
  const uint64_t in4 = pick(inputs, 4);

  switch (type) {
    case GateType::INV:
      return ~in0;
    case GateType::BUF:
    case GateType::INPUT:
    // Wrapper boundary cells are plain buffers in FUNCTIONAL mode (and as a
    // pure gate function). INTEST/EXTEST safe-0 and stimulus-passthrough are
    // applied by the engine via ModeConfig, not here.
    case GateType::WBR_IN:
    case GateType::WBR_OUT:
      return in0;
    case GateType::AND2:
      return in0 & in1;
    case GateType::AND2B:
      return in0 & ~in1;
    case GateType::AND3:
      return in0 & in1 & in2;
    case GateType::AND4:
      return in0 & in1 & in2 & in3;
    case GateType::OR2:
      return in0 | in1;
    case GateType::OR3:
      return in0 | in1 | in2;
    case GateType::OR4:
      return in0 | in1 | in2 | in3;
    case GateType::NAND2:
      return ~(in0 & in1);
    case GateType::NAND3:
      return ~(in0 & in1 & in2);
    case GateType::NAND4:
      return ~(in0 & in1 & in2 & in3);
    case GateType::NOR2:
      return ~(in0 | in1);
    case GateType::NOR3:
      return ~(in0 | in1 | in2);
    case GateType::NOR4:
      return ~(in0 | in1 | in2 | in3);
    case GateType::XOR2:
      return in0 ^ in1;
    case GateType::XOR3:
      return in0 ^ in1 ^ in2;
    case GateType::XNOR2:
      return ~(in0 ^ in1);
    case GateType::XNOR3:
      return ~(in0 ^ in1 ^ in2);
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
    case GateType::MUX2_NI:
      return (~in2 & in0) | (in2 & in1);
    case GateType::A21O:
      return (in0 & in1) | in2;
    case GateType::A21OI:
      return ~((in0 & in1) | in2);
    case GateType::A21BO:
      return (in0 & in1) | ~in2;
    case GateType::A21BOI:
      return ~((in0 & in1) | ~in2);
    case GateType::A22O:
      return (in0 & in1) | (in2 & in3);
    case GateType::A22OI:
      return ~((in0 & in1) | (in2 & in3));
    case GateType::A211OI:
      return ~((in0 & in1) | in2 | in3);
    case GateType::A2111OI:
      return ~((in0 & in1) | in2 | in3 | in4);
    case GateType::A221O:
      return (in0 & in1) | (in2 & in3) | in4;
    case GateType::A221OI:
      return ~((in0 & in1) | (in2 & in3) | in4);
    case GateType::A31O:
      return (in0 & in1 & in2) | in3;
    case GateType::A31OI:
      return ~((in0 & in1 & in2) | in3);
    case GateType::A32O:
      return (in0 & in1 & in2) | (in3 & in4);
    case GateType::A32OI:
      return ~((in0 & in1 & in2) | (in3 & in4));
    case GateType::A41OI:
      return ~((in0 & in1 & in2 & in3) | in4);
    case GateType::O21A:
      return (in0 | in1) & in2;
    case GateType::O21AI:
      return ~((in0 | in1) & in2);
    case GateType::O21BAI:
      return ~((in0 | in1) & ~in2);
    case GateType::O22A:
      return (in0 | in1) & (in2 | in3);
    case GateType::O22AI:
      return ~((in0 | in1) & (in2 | in3));
    case GateType::O211AI:
      return ~((in0 | in1) & in2 & in3);
    case GateType::O221AI:
      return ~((in0 | in1) & (in2 | in3) & in4);
    case GateType::O31A:
      return (in0 | in1 | in2) & in3;
    case GateType::O31AI:
      return ~((in0 | in1 | in2) & in3);
    case GateType::O311A:
      return (in0 | in1 | in2) & in3 & in4;
    case GateType::O311AI:
      return ~((in0 | in1 | in2) & in3 & in4);
    case GateType::O32AI:
      return ~((in0 | in1 | in2) & (in3 | in4));
    case GateType::NAND2B:
      return in0 | ~in1;
    case GateType::NAND3B:
      return in0 | ~in1 | ~in2;
    case GateType::NAND4B:
      return in0 | ~in1 | ~in2 | ~in3;
    case GateType::NOR2B:
      return ~in0 & in1;
    case GateType::NOR3B:
      return ~in0 & ~in1 & in2;
    case GateType::NOR4B:
      return ~in0 & ~in1 & ~in2 & in3;
    case GateType::OR3B:
      return in0 | in1 | ~in2;
    case GateType::OR4B:
      return in0 | in1 | in2 | ~in3;
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
      throw ParseError("Unsupported gate type in eval_gate");
  }
}

bool eval_scalar(GateType type, const std::vector<bool>& inputs) {
  std::vector<uint64_t> words;
  words.reserve(inputs.size());
  for (bool input : inputs) {
    words.push_back(input ? ~0ULL : 0ULL);
  }
  const uint64_t w = eval_bitwise(type, words);
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
