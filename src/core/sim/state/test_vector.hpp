#pragma once

#include <map>

namespace faultflow {

struct TestVector {
  std::map<int, bool> inputs;  // YosysNetID -> value
};

}  // namespace faultflow
