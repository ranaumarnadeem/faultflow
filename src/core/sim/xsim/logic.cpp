#include "sim/xsim/logic.hpp"

#include "sim/gate_eval.hpp"

namespace faultflow {

Logic eval_gate_x(GateType type, const std::vector<Logic>& inputs) {
  std::vector<bool> bits(inputs.size());
  std::vector<size_t> unknown;
  for (size_t i = 0; i < inputs.size(); ++i) {
    if (is_known(inputs[i])) {
      bits[i] = inputs[i] == Logic::ONE;
    } else {
      unknown.push_back(i);
    }
  }
  bool seen0 = false;
  bool seen1 = false;
  for (size_t m = 0; m < (size_t{1} << unknown.size()); ++m) {
    for (size_t k = 0; k < unknown.size(); ++k) {
      bits[unknown[k]] = ((m >> k) & 1) != 0;
    }
    (eval_gate_scalar(type, bits) ? seen1 : seen0) = true;
    if (seen0 && seen1) {
      return Logic::X;
    }
  }
  return logic_of(seen1);
}

Logic eval_icg_x(GateType type, Logic clk, Logic enable, Logic& latch) {
  if (type == GateType::ICG_POS) {
    latch = logic_mux(clk, enable, latch);
    return logic_and(clk, latch);
  }
  latch = logic_mux(clk, latch, enable);
  return logic_or(clk, logic_not(latch));
}

}  // namespace faultflow
