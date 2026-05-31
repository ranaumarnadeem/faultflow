#include <catch2/catch_test_macros.hpp>

#include "fault/batch/batch_manager.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"

using namespace faultflow;

TEST_CASE("c17 loads and compiles", "[c17][integration]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  REQUIRE(ng.PIs.size() == 5);
  REQUIRE(ng.POs.size() == 2);
  int logic_gates = 0;
  for (const auto& sn : cg.nodes) {
    if (sn.type != GateType::INPUT && sn.type != GateType::BUF) {
      ++logic_gates;
    }
  }
  REQUIRE(logic_gates == 6);
}

TEST_CASE("c17 exhaustive coverage and golden parity", "[c17][integration]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  const auto faults = enumerate_faults(ng, cg);
  std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
  std::sort(pi_ids.begin(), pi_ids.end());
  const test::VectorSet vs = test::generate_complete_input_space(pi_ids);
  REQUIRE(vs.vectors.size() == 32);

  const auto mismatches =
      test::verify_parallel_matches_golden(cg, ng, faults, vs);
  REQUIRE(mismatches.empty());

  const double coverage = test::run_coverage_exhaustive(cg, ng, vs);
  REQUIRE(coverage >= 99.0);
  REQUIRE(coverage <= 100.0);
}

TEST_CASE("c17 batching matches golden detection counts", "[c17][integration]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth/c17.json");
  const auto all_faults = enumerate_faults(ng, cg);
  std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
  std::sort(pi_ids.begin(), pi_ids.end());
  const test::VectorSet vs = test::generate_complete_input_space(pi_ids);

  GoldenRefSim golden;
  BitParallelSim parallel;
  size_t golden_detected = 0;
  for (const auto& fault : all_faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    for (const auto& vec : vs.vectors) {
      const auto ff = golden.simulate_fault_free(cg, vec);
      const auto fa = golden.simulate_with_fault(cg, vec, fault);
      if (golden.is_detected(cg, ff, fa)) {
        ++golden_detected;
        break;
      }
    }
  }

  BatchManager bm(all_faults);
  while (bm.has_pending()) {
    FaultBatch batch = bm.next_batch();
    uint64_t batch_detected = 0;
    for (const auto& vec : vs.vectors) {
      batch_detected |= parallel.simulate_batch(cg, vec, batch);
    }
    bm.finish_batch(batch, batch_detected);
  }

  REQUIRE(bm.pending_count() == 0);
  REQUIRE(bm.detected_count() == golden_detected);
}
