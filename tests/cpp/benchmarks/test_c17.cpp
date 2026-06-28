#include <catch2/catch_test_macros.hpp>

#include "fault/batch/batch_manager.hpp"
#include "fault/effect/fault_batch.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"

using namespace faultflow;

TEST_CASE("c17 loads and compiles", "[c17][integration]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  REQUIRE(ng.PIs.size() == 5);
  REQUIRE(ng.POs.size() == 2);
  int logic_gates = 0;
  for (const auto& sn : cg.nodes) {
    if (sn.type != GateType::INPUT && sn.type != GateType::BUF) {
      ++logic_gates;
    }
  }
  REQUIRE(logic_gates == 3);
}

TEST_CASE("c17 exhaustive coverage and golden parity", "[c17][integration]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
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
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
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

// Stronger than the count check above: pack EVERY active c17 fault into a single
// 64-lane batch so each net's SA0 and SA1 (and every fanout branch) share the
// batch, then verify each fault's individual detection lane against GoldenRefSim.
// This is the dedicated guard for the per-net fault-present injection gate: if the
// gate ever injected only the first fault landing on a net and skipped the rest,
// the second fault's lane would diverge from golden here even though the aggregate
// count might still match.
TEST_CASE("c17 per-fault batch detection matches golden (injection gating)",
          "[c17][integration][inject_gate]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const auto all_faults = enumerate_faults(ng, cg);
  std::vector<int> pi_ids(ng.PIs.begin(), ng.PIs.end());
  std::sort(pi_ids.begin(), pi_ids.end());
  const test::VectorSet vs = test::generate_complete_input_space(pi_ids);

  std::vector<CompactFault> active;
  for (const auto& f : all_faults) {
    if (f.exclusion == FaultExclusion::NONE) {
      active.push_back(f);
    }
  }
  // c17 has both polarities on every fault site, so the batch must hold multiple
  // faults per net; assert that precondition actually holds (and fits one batch).
  REQUIRE(active.size() > 1);
  REQUIRE(active.size() <= static_cast<size_t>(kBatchSize));

  FaultBatch batch;
  batch.size = static_cast<int>(active.size());
  batch.mask = 0;
  for (int j = 0; j < batch.size; ++j) {
    CompactFault f = active[static_cast<size_t>(j)];
    f.bit = static_cast<uint8_t>(j + 1);  // lanes 1..63; bit 0 = fault-free
    f.sa_mask = 1ULL << f.bit;
    batch.faults[j] = f;
    batch.mask |= f.sa_mask;
  }
  // Confirm the batch genuinely stacks >=2 faults on at least one net.
  bool any_net_shared = false;
  for (int a = 0; a < batch.size && !any_net_shared; ++a) {
    for (int b = a + 1; b < batch.size; ++b) {
      if (batch.faults[a].net_index == batch.faults[b].net_index) {
        any_net_shared = true;
        break;
      }
    }
  }
  REQUIRE(any_net_shared);

  BitParallelSim parallel;
  uint64_t detected = 0;
  for (const auto& vec : vs.vectors) {
    detected |= parallel.simulate_batch(cg, vec, batch);
  }

  GoldenRefSim golden;
  for (int j = 0; j < batch.size; ++j) {
    const CompactFault& f = batch.faults[j];
    const bool parallel_detected = ((detected >> f.bit) & 1ULL) != 0;
    bool golden_detected = false;
    for (const auto& vec : vs.vectors) {
      const auto ff = golden.simulate_fault_free(cg, vec);
      const auto fa = golden.simulate_with_fault(cg, vec, f);
      if (golden.is_detected(cg, ff, fa)) {
        golden_detected = true;
        break;
      }
    }
    INFO("fault net_index=" << f.net_index
                            << " type=" << static_cast<int>(f.type)
                            << " lane=" << static_cast<int>(f.bit));
    REQUIRE(parallel_detected == golden_detected);
  }
}
