#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("Primitive collapser marks INV input equivalents", "[collapser]") {
  const NormalizedGraph ng = test::load_normalized("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const auto collapsed = std::count_if(
      faults.begin(), faults.end(),
      [](const CompactFault& f) { return f.collapsed_into != UINT32_MAX; });

  REQUIRE(collapsed >= 2);
}
