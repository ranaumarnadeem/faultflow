#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <limits>
#include <vector>

#include "fault/effect/fault_batch.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"

using namespace faultflow;

namespace {

// Build a TestVector with explicit YosysNetID->value input mapping.
TestVector make_vec(std::initializer_list<std::pair<int, bool>> entries) {
  TestVector v;
  for (const auto& [k, val] : entries) {
    v.inputs[k] = val;
  }
  return v;
}

// Find the first transition fault matching (net_index, type), or nullptr.
const CompactFault* find_transition_fault(const std::vector<CompactFault>& faults,
                                           uint32_t net_index, FaultType type) {
  for (const auto& f : faults) {
    if (f.net_index == net_index && f.type == type) return &f;
  }
  return nullptr;
}

struct TransitionMismatch {
  uint32_t fault_net;
  FaultType fault_type;
  size_t v1_index;
  size_t v2_index;
  bool golden_detected;
  bool parallel_detected;
};

// Exhaustive golden==parallel check for all active transition faults over all V1×V2 pairs.
std::vector<TransitionMismatch> verify_transition_parallel_matches_golden(
    const CompiledSimGraph& cg, const std::vector<CompactFault>& faults,
    const test::VectorSet& all_vectors) {
  GoldenRefSim golden;
  BitParallelSim parallel;
  std::vector<TransitionMismatch> mismatches;

  for (const auto& f : faults) {
    if (f.exclusion != FaultExclusion::NONE) continue;
    for (size_t i = 0; i < all_vectors.vectors.size(); ++i) {
      for (size_t j = 0; j < all_vectors.vectors.size(); ++j) {
        const auto& v1 = all_vectors.vectors[i];
        const auto& v2 = all_vectors.vectors[j];
        const bool g = golden.simulate_transition_fault(cg, v1, v2, f);
        const bool p = parallel.simulate_transition_single_fault(cg, v1, v2, f);
        if (g != p) {
          mismatches.push_back({f.net_index, f.type, i, j, g, p});
        }
      }
    }
  }
  return mismatches;
}

}  // namespace

// ---------------------------------------------------------------------------
// Regression: adding FaultModel + CompactFault.model does NOT change stuck-at
// ---------------------------------------------------------------------------

TEST_CASE("CompactFault model field defaults to STUCK_AT", "[transition][regression]") {
  CompactFault f;
  REQUIRE(f.model == FaultModel::STUCK_AT);

  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = enumerate_faults(ng, cg);

  for (const auto& fault : faults) {
    REQUIRE(fault.model == FaultModel::STUCK_AT);
  }
}

// ---------------------------------------------------------------------------
// Transition fault enumeration
// ---------------------------------------------------------------------------

TEST_CASE("Transition fault enumeration: STR/STF per net, same exclusions",
          "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");

  const auto sa_faults = enumerate_faults(ng, cg);
  const auto tr_faults = enumerate_transition_faults(ng, cg);

  // Same number of faults
  REQUIRE(tr_faults.size() == sa_faults.size());

  // All transition faults carry the TRANSITION model
  for (const auto& f : tr_faults) {
    REQUIRE(f.model == FaultModel::TRANSITION);
  }

  // Exclusions are identical between SA and transition fault lists
  for (size_t i = 0; i < sa_faults.size(); ++i) {
    REQUIRE(tr_faults[i].exclusion == sa_faults[i].exclusion);
    REQUIRE(tr_faults[i].net_index == sa_faults[i].net_index);
    REQUIRE(tr_faults[i].type == sa_faults[i].type);
  }
}

TEST_CASE("Transition enumeration on clock_reset fixture preserves exclusion tags",
          "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_clock_reset.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_clock_reset.json");

  const auto sa_faults = enumerate_faults(ng, cg);
  const auto tr_faults = enumerate_transition_faults(ng, cg);

  REQUIRE(tr_faults.size() == sa_faults.size());
  for (size_t i = 0; i < sa_faults.size(); ++i) {
    REQUIRE(tr_faults[i].exclusion == sa_faults[i].exclusion);
  }
  // At least some faults should be excluded as clock/reset
  const auto excluded = std::count_if(tr_faults.begin(), tr_faults.end(),
                                       [](const CompactFault& f) {
                                         return f.exclusion != FaultExclusion::NONE;
                                       });
  REQUIRE(excluded > 0);
}

// ---------------------------------------------------------------------------
// STR detection with valid init/capture pair
// tiny_and2: A=net2, B=net3, Y=net4.  Y = A & B.
// STR at Y (SA0): V1=(A=0,B=0)->Y=0 ; V2=(A=1,B=1)->Y=1
// ---------------------------------------------------------------------------

TEST_CASE("Transition STR detected with valid init/capture pair", "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");

  const uint32_t ci_Y = static_cast<uint32_t>(cg.yosys_to_compiled.at(4));

  CompactFault f;
  f.net_index = ci_Y;
  f.type = FaultType::SA0;  // STR
  f.model = FaultModel::TRANSITION;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  f.exclusion = FaultExclusion::NONE;
  f.collapsed_into = std::numeric_limits<uint32_t>::max();

  // V1: Y goes 0 (the pre-transition value)
  const TestVector v1 = make_vec({{2, false}, {3, false}});
  // V2: Y goes 1 (the post-transition value)
  const TestVector v2 = make_vec({{2, true}, {3, true}});

  GoldenRefSim golden;
  REQUIRE(golden.simulate_transition_fault(cg, v1, v2, f) == true);

  BitParallelSim parallel;
  REQUIRE(parallel.simulate_transition_single_fault(cg, v1, v2, f) == true);

  // Both must agree
  REQUIRE(golden.simulate_transition_fault(cg, v1, v2, f) ==
          parallel.simulate_transition_single_fault(cg, v1, v2, f));
}

// ---------------------------------------------------------------------------
// STF detection with valid init/capture pair
// STF at Y (SA1): V1=(A=1,B=1)->Y=1 ; V2=(A=0,B=0)->Y=0
// ---------------------------------------------------------------------------

TEST_CASE("Transition STF detected with valid init/capture pair", "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");

  const uint32_t ci_Y = static_cast<uint32_t>(cg.yosys_to_compiled.at(4));

  CompactFault f;
  f.net_index = ci_Y;
  f.type = FaultType::SA1;  // STF
  f.model = FaultModel::TRANSITION;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  f.exclusion = FaultExclusion::NONE;
  f.collapsed_into = std::numeric_limits<uint32_t>::max();

  // V1: Y goes 1 (pre-transition)
  const TestVector v1 = make_vec({{2, true}, {3, true}});
  // V2: Y goes 0 (post-transition)
  const TestVector v2 = make_vec({{2, false}, {3, false}});

  GoldenRefSim golden;
  REQUIRE(golden.simulate_transition_fault(cg, v1, v2, f) == true);

  BitParallelSim parallel;
  REQUIRE(parallel.simulate_transition_single_fault(cg, v1, v2, f) == true);

  REQUIRE(golden.simulate_transition_fault(cg, v1, v2, f) ==
          parallel.simulate_transition_single_fault(cg, v1, v2, f));
}

// ---------------------------------------------------------------------------
// No detection when good machine does not transition
// ---------------------------------------------------------------------------

TEST_CASE("No detection when good machine does not transition", "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");

  const uint32_t ci_Y = static_cast<uint32_t>(cg.yosys_to_compiled.at(4));

  // STR (SA0) at Y — requires 0->1 transition in good machine
  CompactFault str;
  str.net_index = ci_Y;
  str.type = FaultType::SA0;
  str.model = FaultModel::TRANSITION;
  str.bit = 1;
  str.sa_mask = 1ULL << 1;
  str.exclusion = FaultExclusion::NONE;
  str.collapsed_into = std::numeric_limits<uint32_t>::max();

  GoldenRefSim golden;

  // Both cycles: Y stays 0 (no transition)
  const TestVector v_zero = make_vec({{2, false}, {3, false}});
  REQUIRE(golden.simulate_transition_fault(cg, v_zero, v_zero, str) == false);

  // Both cycles: Y stays 1 (net already high — STR has no missing rising edge)
  const TestVector v_one = make_vec({{2, true}, {3, true}});
  REQUIRE(golden.simulate_transition_fault(cg, v_one, v_one, str) == false);

  // STF (SA1) at Y — requires 1->0 transition in good machine
  CompactFault stf;
  stf.net_index = ci_Y;
  stf.type = FaultType::SA1;
  stf.model = FaultModel::TRANSITION;
  stf.bit = 1;
  stf.sa_mask = 1ULL << 1;
  stf.exclusion = FaultExclusion::NONE;
  stf.collapsed_into = std::numeric_limits<uint32_t>::max();

  // Both cycles: Y stays 0 (no falling edge)
  REQUIRE(golden.simulate_transition_fault(cg, v_zero, v_zero, stf) == false);
  // Both cycles: Y stays 1 (no falling edge)
  REQUIRE(golden.simulate_transition_fault(cg, v_one, v_one, stf) == false);

  // Also verify bit-parallel agrees
  BitParallelSim parallel;
  REQUIRE(parallel.simulate_transition_single_fault(cg, v_zero, v_zero, str) == false);
  REQUIRE(parallel.simulate_transition_single_fault(cg, v_one, v_one, str) == false);
  REQUIRE(parallel.simulate_transition_single_fault(cg, v_zero, v_zero, stf) == false);
  REQUIRE(parallel.simulate_transition_single_fault(cg, v_one, v_one, stf) == false);
}

// ---------------------------------------------------------------------------
// Excluded faults always return false (regression: exclusion still respected)
// ---------------------------------------------------------------------------

TEST_CASE("Excluded transition fault is never detected", "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");

  const uint32_t ci_Y = static_cast<uint32_t>(cg.yosys_to_compiled.at(4));

  CompactFault f;
  f.net_index = ci_Y;
  f.type = FaultType::SA0;
  f.model = FaultModel::TRANSITION;
  f.bit = 1;
  f.sa_mask = 1ULL << 1;
  f.exclusion = FaultExclusion::CLOCK;  // excluded
  f.collapsed_into = std::numeric_limits<uint32_t>::max();

  const TestVector v1 = make_vec({{2, false}, {3, false}});
  const TestVector v2 = make_vec({{2, true}, {3, true}});

  GoldenRefSim golden;
  BitParallelSim parallel;

  REQUIRE(golden.simulate_transition_fault(cg, v1, v2, f) == false);
  REQUIRE(parallel.simulate_transition_single_fault(cg, v1, v2, f) == false);
}

// ---------------------------------------------------------------------------
// Exhaustive golden == parallel on tiny_and2 (2 PIs, 4×4 = 16 V1×V2 pairs)
// ---------------------------------------------------------------------------

TEST_CASE("Bit-parallel matches golden for all transition faults on tiny_and2",
          "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = enumerate_transition_faults(ng, cg);

  std::vector<int> pi_yids;
  for (int cidx : cg.pi_nets) {
    pi_yids.push_back(cg.compiled_to_yosys[static_cast<size_t>(cidx)]);
  }
  const auto all_vectors = test::generate_complete_input_space(pi_yids);

  const auto mismatches =
      verify_transition_parallel_matches_golden(cg, faults, all_vectors);

  REQUIRE(mismatches.empty());
}

// ---------------------------------------------------------------------------
// Exhaustive golden == parallel on tiny_chain (3 PIs, 8×8 = 64 V1×V2 pairs)
// tiny_chain: A=net2, B=net3, C=net4, Y=net7
//   u0: INV(A) -> n5   u1: AND2(n5,B) -> n6   u2: NOR2(n6,C) -> Y
// ---------------------------------------------------------------------------

TEST_CASE("Bit-parallel matches golden for all transition faults on tiny_chain",
          "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_chain.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_chain.json");
  const auto faults = enumerate_transition_faults(ng, cg);

  std::vector<int> pi_yids;
  for (int cidx : cg.pi_nets) {
    pi_yids.push_back(cg.compiled_to_yosys[static_cast<size_t>(cidx)]);
  }
  const auto all_vectors = test::generate_complete_input_space(pi_yids);

  const auto mismatches =
      verify_transition_parallel_matches_golden(cg, faults, all_vectors);

  REQUIRE(mismatches.empty());
}

// ---------------------------------------------------------------------------
// Verified STR/STF pair on tiny_chain (spot check for multi-level propagation)
// Y = ~((~A & B) | C)
// STR at Y:  V1: A=1,B=1,C=1 -> Y=~(0|1)=0 ; V2: A=1,B=0,C=0 -> Y=~(0|0)=1
// STF at Y:  V1: A=1,B=0,C=0 -> Y=1        ; V2: A=1,B=1,C=1 -> Y=0
// ---------------------------------------------------------------------------

TEST_CASE("Transition STR/STF detected on tiny_chain multi-level", "[transition]") {
  const NormalizedGraph ng = test::load_normalized("tiny_chain.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_chain.json");

  const uint32_t ci_Y = static_cast<uint32_t>(cg.yosys_to_compiled.at(7));

  auto make_fault = [&](FaultType t) {
    CompactFault f;
    f.net_index = ci_Y;
    f.type = t;
    f.model = FaultModel::TRANSITION;
    f.bit = 1;
    f.sa_mask = 1ULL << 1;
    f.exclusion = FaultExclusion::NONE;
    f.collapsed_into = std::numeric_limits<uint32_t>::max();
    return f;
  };

  GoldenRefSim golden;
  BitParallelSim parallel;

  // STR at Y
  const TestVector str_v1 = make_vec({{2, true}, {3, true}, {4, true}});   // Y=0
  const TestVector str_v2 = make_vec({{2, true}, {3, false}, {4, false}}); // Y=1
  const CompactFault str_f = make_fault(FaultType::SA0);
  REQUIRE(golden.simulate_transition_fault(cg, str_v1, str_v2, str_f) == true);
  REQUIRE(parallel.simulate_transition_single_fault(cg, str_v1, str_v2, str_f) == true);

  // STF at Y
  const TestVector stf_v1 = make_vec({{2, true}, {3, false}, {4, false}}); // Y=1
  const TestVector stf_v2 = make_vec({{2, true}, {3, true}, {4, true}});   // Y=0
  const CompactFault stf_f = make_fault(FaultType::SA1);
  REQUIRE(golden.simulate_transition_fault(cg, stf_v1, stf_v2, stf_f) == true);
  REQUIRE(parallel.simulate_transition_single_fault(cg, stf_v1, stf_v2, stf_f) == true);
}

// ---------------------------------------------------------------------------
// Batch transition simulation == single-fault == golden (guards the batch
// over-detection / transition-qualification logic in simulate_transition_batch)
// ---------------------------------------------------------------------------

namespace {

// Pack a slice [begin,end) of active faults into a FaultBatch (lane bits 1..63).
FaultBatch make_transition_batch(const std::vector<CompactFault>& faults,
                                 size_t begin, size_t end) {
  FaultBatch batch;
  batch.size = static_cast<int>(end - begin);
  for (size_t i = begin; i < end; ++i) {
    CompactFault lane = faults[i];
    lane.bit = static_cast<uint8_t>((i - begin) + 1);
    lane.sa_mask = 1ULL << lane.bit;
    batch.faults[i - begin] = lane;
    batch.mask |= lane.sa_mask;
  }
  return batch;
}

// For every V1xV2 pair and every active fault, assert the batch detection mask
// agrees bit-for-bit with the single-fault engine and the golden oracle.
void check_batch_parity(const CompiledSimGraph& cg,
                        const std::vector<CompactFault>& all_faults,
                        const test::VectorSet& vectors) {
  std::vector<CompactFault> active;
  for (const auto& f : all_faults) {
    if (f.exclusion == FaultExclusion::NONE) active.push_back(f);
  }

  GoldenRefSim golden;
  BitParallelSim parallel;
  for (const auto& v1 : vectors.vectors) {
    for (const auto& v2 : vectors.vectors) {
      for (size_t begin = 0; begin < active.size();
           begin += static_cast<size_t>(kBatchSize)) {
        const size_t end =
            std::min(begin + static_cast<size_t>(kBatchSize), active.size());
        const FaultBatch batch = make_transition_batch(active, begin, end);
        const uint64_t mask =
            parallel.simulate_transition_batch(cg, v1, v2, batch);
        for (size_t i = begin; i < end; ++i) {
          const CompactFault& lane = batch.faults[i - begin];
          const bool batch_detected = (mask & lane.sa_mask) != 0;
          const bool single =
              parallel.simulate_transition_single_fault(cg, v1, v2, active[i]);
          const bool gold =
              golden.simulate_transition_fault(cg, v1, v2, active[i]);
          REQUIRE(batch_detected == single);
          REQUIRE(batch_detected == gold);
        }
      }
    }
  }
}

}  // namespace

TEST_CASE("simulate_transition_batch == single == golden on tiny fixtures",
          "[transition]") {
  for (const char* fixture : {"tiny_and2.json", "tiny_chain.json",
                              "tiny_or2.json", "tiny_nor2.json"}) {
    const NormalizedGraph ng = test::load_normalized(fixture);
    const CompiledSimGraph cg = test::load_compiled(fixture);
    const auto faults = enumerate_transition_faults(ng, cg);

    std::vector<int> pi_yids;
    for (int cidx : cg.pi_nets) {
      pi_yids.push_back(cg.compiled_to_yosys[static_cast<size_t>(cidx)]);
    }
    const auto vectors = test::generate_complete_input_space(pi_yids);
    check_batch_parity(cg, faults, vectors);
  }
}

TEST_CASE("simulate_transition_batch == single == golden on c17",
          "[transition]") {
  const NormalizedGraph ng =
      test::load_normalized_benchmark("iscas85/synth_sky130/c17.json");
  const CompiledSimGraph cg =
      test::load_compiled_benchmark("iscas85/synth_sky130/c17.json");
  const auto faults = enumerate_transition_faults(ng, cg);

  std::vector<int> pi_yids;
  for (int cidx : cg.pi_nets) {
    pi_yids.push_back(cg.compiled_to_yosys[static_cast<size_t>(cidx)]);
  }
  const auto vectors = test::generate_complete_input_space(pi_yids);
  check_batch_parity(cg, faults, vectors);
}
