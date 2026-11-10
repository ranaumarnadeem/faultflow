#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <filesystem>
#include <random>
#include <string>
#include <vector>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/xsim/bit_parallel_sim_x.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"

using namespace faultflow;

namespace {

std::vector<std::string> loadable_fixtures() {
  std::vector<std::string> out;
  for (const auto& entry : std::filesystem::directory_iterator(FAULTFLOW_FIXTURE_DIR)) {
    const std::string name = entry.path().filename().string();
    if (name.rfind("tiny_", 0) != 0 || entry.path().extension() != ".json") {
      continue;
    }
    try {
      (void)test::load_compiled(name);
      out.push_back(name);
    } catch (const std::exception&) {
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

Logic lane(const Dual& d, int i) {
  const bool h = ((d.h >> i) & 1ULL) != 0;
  const bool l = ((d.l >> i) & 1ULL) != 0;
  if (h && l) {
    return Logic::X;
  }
  if (h) {
    return Logic::ONE;
  }
  return l ? Logic::ZERO : Logic::Z;
}

// Batches of up to 63 faults, lanes 1..63, every fault active (the bit-parallel
// engines inject whatever a batch holds).
std::vector<FaultBatch> batches_of(std::vector<CompactFault> faults) {
  std::vector<FaultBatch> out;
  for (size_t i = 0; i < faults.size(); i += kBatchSize) {
    FaultBatch batch;
    for (size_t j = i; j < std::min(faults.size(), i + kBatchSize); ++j) {
      CompactFault f = faults[j];
      f.exclusion = FaultExclusion::NONE;
      f.bit = static_cast<uint8_t>(batch.size + 1);
      f.sa_mask = 1ULL << f.bit;
      batch.faults[batch.size++] = f;
      batch.mask |= f.sa_mask;
    }
    out.push_back(batch);
  }
  return out;
}

struct Case {
  TestVector vec;
  XRunOptions options;
};

Case random_case(const CompiledSimGraph& cg, std::mt19937& rng, bool with_x) {
  Case c;
  for (int k = 0; k < 7; ++k) {
    TestCycle tc;
    for (int pi : cg.pi_nets) {
      tc.inputs[cg.compiled_to_yosys[pi]] = (rng() & 1) != 0;
    }
    tc.sample_outputs = (rng() % 3) != 0;
    tc.fault_active = (rng() % 4) != 0;
    c.vec.cycles.push_back(tc);
    std::set<int> unknown;
    if (with_x) {
      for (int pi : cg.pi_nets) {
        if (rng() % 5 == 0) {
          unknown.insert(cg.compiled_to_yosys[pi]);
        }
      }
    }
    c.options.unknown_inputs.push_back(unknown);
  }
  for (uint32_t ff = 0; ff < cg.ff_configs.size(); ++ff) {
    if (rng() & 1) {
      c.vec.initial_ff_state[ff] = (rng() & 1) != 0;
    }
  }
  c.options.initial_state = with_x ? Logic::X : Logic::ZERO;
  return c;
}

}  // namespace

TEST_CASE("The X engine equals the X reference on every lane, net and sample",
          "[xsim][bit_parallel_x]") {
  const auto fixtures = loadable_fixtures();
  REQUIRE(fixtures.size() > 30);
  GoldenRefSimX ref;
  BitParallelSimX sim;
  std::mt19937 rng(20261111);
  for (const std::string& name : fixtures) {
    INFO(name);
    const NormalizedGraph ng = test::load_normalized(name);
    const CompiledSimGraph cg = test::load_compiled(name);
    for (const FaultBatch& batch : batches_of(enumerate_faults(ng, cg))) {
      for (int seq = 0; seq < 4; ++seq) {
        const Case c = random_case(cg, rng, true);
        const auto got = sim.simulate_batch_samples(cg, c.vec, batch, c.options);
        const auto good = ref.simulate_sequence(cg, c.vec, nullptr, c.options);
        REQUIRE(got.size() == good.size());
        const XBatchDetection det = sim.detect_batch(cg, c.vec, batch, c.options);
        for (int b = 0; b <= batch.size; ++b) {
          const CompactFault* fault = b == 0 ? nullptr : &batch.faults[b - 1];
          const auto want = b == 0 ? good : ref.simulate_sequence(cg, c.vec, fault, c.options);
          for (size_t s = 0; s < want.size(); ++s) {
            for (const auto& [yid, cidx] : cg.yosys_to_compiled) {
              REQUIRE(lane(got[s][static_cast<size_t>(cidx)], b) == want[s].at(yid));
            }
          }
          if (b > 0) {
            const XDetection cls = classify_detection(cg, good, want);
            const uint64_t bit = 1ULL << b;
            REQUIRE(((det.definite & bit) != 0) == (cls == XDetection::DEFINITE));
            REQUIRE(((det.possible & bit) != 0) == (cls == XDetection::POSSIBLE));
          }
        }
      }
    }
  }
}

TEST_CASE("With no X source the X engine's rails are the two-valued engine's words",
          "[xsim][bit_parallel_x]") {
  BitParallelSim two;
  BitParallelSimX three;
  std::mt19937 rng(5);
  for (const std::string& name : loadable_fixtures()) {
    INFO(name);
    const NormalizedGraph ng = test::load_normalized(name);
    const CompiledSimGraph cg = test::load_compiled(name);
    for (const FaultBatch& batch : batches_of(enumerate_faults(ng, cg))) {
      for (int seq = 0; seq < 4; ++seq) {
        const Case c = random_case(cg, rng, false);
        const auto want = two.simulate_batch_samples(cg, c.vec, batch);
        const auto got = three.simulate_batch_samples(cg, c.vec, batch, c.options);
        REQUIRE(got.size() == want.size());
        for (size_t s = 0; s < want.size(); ++s) {
          for (size_t n = 0; n < want[s].size(); ++n) {
            REQUIRE(got[s][n].h == want[s][n]);
            REQUIRE(got[s][n].l == ~want[s][n]);
          }
        }
        // And every two-valued detection is a definite one, nothing possible.
        const XBatchDetection det = three.detect_batch(cg, c.vec, batch, c.options);
        REQUIRE(det.definite == two.simulate_batch(cg, c.vec, batch));
        REQUIRE(det.possible == 0ULL);
      }
    }
  }
}
