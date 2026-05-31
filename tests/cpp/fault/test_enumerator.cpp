#include <catch2/catch_test_macros.hpp>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("FaultEnumerator c17", "[enumerator]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  const auto faults = enumerate_faults(ng, cg);
  REQUIRE(faults.size() == static_cast<size_t>(cg.net_count) * 2);
}
