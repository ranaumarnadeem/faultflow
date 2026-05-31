#include "sim/state/test_vector.hpp"

namespace faultflow {

VectorSet generate_complete_input_space(const std::vector<int>& pi_yosys_ids) {
  VectorSet vs;
  const size_t n = pi_yosys_ids.size();
  const size_t count = (n == 0) ? 1 : (1ULL << n);
  for (size_t mask = 0; mask < count; ++mask) {
    TestVector tv;
    for (size_t i = 0; i < n; ++i) {
      tv.inputs[pi_yosys_ids[i]] = ((mask >> i) & 1) != 0;
    }
    vs.vectors.push_back(std::move(tv));
  }
  return vs;
}

}  // namespace faultflow
