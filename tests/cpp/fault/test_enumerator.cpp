#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"

using namespace faultflow;

TEST_CASE("FaultEnumerator c17", "[enumerator]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const auto faults = enumerate_faults(ng, cg);
  REQUIRE(faults.size() == static_cast<size_t>(cg.net_count) * 2);
}

TEST_CASE("FaultEnumerator tags blackbox faults", "[enumerator]") {
  const ParsedGraph pg = test::load_parsed("tiny_blackbox.json");
  const CellMap map = CellMap::load(test::cell_map_path());
  const NormalizedGraph ng = NormalizedGraph::from_parsed(pg, map, "blackbox");
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
  const auto faults = enumerate_faults(ng, cg);
  REQUIRE(faults.size() == static_cast<size_t>(cg.net_count) * 2);
  const int y_cidx = cg.yosys_to_compiled.at(3);
  const auto blackbox_count = std::count_if(
      faults.begin(), faults.end(), [y_cidx](const CompactFault& f) {
        return f.net_index == static_cast<uint32_t>(y_cidx) &&
               f.exclusion == FaultExclusion::BLACKBOX;
      });
  REQUIRE(blackbox_count == 2);
}

TEST_CASE("FaultEnumerator tags clock and reset exclusions", "[enumerator]") {
  const NormalizedGraph ng = test::load_normalized("tiny_clock_reset.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_clock_reset.json");
  const auto faults = enumerate_faults(ng, cg);
  const int clk_cidx = cg.yosys_to_compiled.at(2);
  const int rst_cidx = cg.yosys_to_compiled.at(3);

  REQUIRE(std::count_if(faults.begin(), faults.end(),
                        [clk_cidx](const CompactFault& f) {
                          return f.net_index == static_cast<uint32_t>(clk_cidx) &&
                                 f.exclusion == FaultExclusion::CLOCK;
                        }) == 2);
  REQUIRE(std::count_if(faults.begin(), faults.end(),
                        [rst_cidx](const CompactFault& f) {
                          return f.net_index == static_cast<uint32_t>(rst_cidx) &&
                                 f.exclusion == FaultExclusion::RESET;
                        }) == 2);

  EnumeratorOptions include_special;
  include_special.include_clock_faults = true;
  include_special.include_reset_faults = true;
  const auto included_faults = enumerate_faults(ng, cg, include_special);
  REQUIRE(std::none_of(included_faults.begin(), included_faults.end(),
                       [](const CompactFault& f) {
                         return f.exclusion == FaultExclusion::CLOCK ||
                                f.exclusion == FaultExclusion::RESET;
                       }));
}
