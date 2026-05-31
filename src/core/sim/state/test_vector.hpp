#pragma once

#include <map>
#include <vector>

namespace faultflow {

struct TestVector {
  std::map<int, bool> inputs;  // YosysNetID -> value
};

struct VectorSet {
  std::vector<TestVector> vectors;
};

VectorSet generate_complete_input_space(const std::vector<int>& pi_yosys_ids);

}  // namespace faultflow
