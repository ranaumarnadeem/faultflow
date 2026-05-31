#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("FaultEnumerator tiny_and2", "[enumerator]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = enumerate_faults(ng, cg);
  REQUIRE(faults.size() == 6);
}
