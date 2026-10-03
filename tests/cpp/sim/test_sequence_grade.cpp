#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <array>
#include <string>
#include <vector>

#include "fault/effect/fault_batch.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/engine/sequence_grade.hpp"

using namespace faultflow;

namespace {

// tiny_seq_observe: DIN -> u0 -> u1 -> u2 -> DOUT (dfrtp, RESET_B), EXTRA = ~u0.Q.
const std::string kFixture = "tiny_seq_observe.json";

// Two cycles per clock period: CLK low (sampled), then CLK high (the edge).
// RESET_B is held low for the first two periods, or throughout with reset_held.
SequenceGradeRequest shift_request(const std::vector<std::string>& observe,
                                   bool reset_held = false) {
  SequenceGradeRequest request;
  request.input_order = {"CLK", "RESET_B", "DIN"};
  request.observe_outputs = observe;
  const auto period = [&](bool reset_b, bool din) {
    request.cycles.push_back({false, reset_b, din});
    request.sample.push_back(true);
    request.cycles.push_back({true, reset_b, din});
    request.sample.push_back(false);
  };
  period(false, false);
  period(false, true);
  for (const int din : {1, 0, 1, 1, 0, 0, 1, 0, 0, 0}) {
    period(!reset_held, din != 0);
  }
  return request;
}

std::vector<SequenceFaultSpec> every_fault() {
  const CachedGraph& graph =
      load_cached_graph(test::fixture_path(kFixture), test::cell_map_path(), "fail");
  std::vector<SequenceFaultSpec> faults;
  for (uint32_t net = 0; net < static_cast<uint32_t>(graph.cg.net_count); ++net) {
    faults.push_back({net, 0});
    faults.push_back({net, 1});
  }
  return faults;
}

SequenceGradeResult grade(SequenceGradeRequest request,
                          const std::vector<SequenceFaultSpec>& faults,
                          int threads = 1, bool reference = false) {
  request.faults = faults;
  return grade_sequence_faults(test::fixture_path(kFixture), test::cell_map_path(),
                               request, "fail", {}, threads, reference);
}

uint32_t compiled_net(const std::string& name) {
  const CachedGraph& graph =
      load_cached_graph(test::fixture_path(kFixture), test::cell_map_path(), "fail");
  return static_cast<uint32_t>(
      graph.cg.yosys_to_compiled.at(graph.parsed.net_id_by_name(name)));
}

}  // namespace

TEST_CASE("Sequence grading: bit-parallel matches the scalar reference",
          "[sequence_grade]") {
  const auto faults = every_fault();
  for (const auto& observe : std::vector<std::vector<std::string>>{
           {"DOUT"}, {"EXTRA"}, {"DOUT", "EXTRA"}}) {
    const SequenceGradeResult fast = grade(shift_request(observe), faults);
    const SequenceGradeResult slow = grade(shift_request(observe), faults, 1, true);
    REQUIRE(fast.golden == slow.golden);
    REQUIRE(fast.first_sample == slow.first_sample);
  }
}

TEST_CASE("Sequence grading: the shift register's good machine and first detections",
          "[sequence_grade]") {
  const SequenceGradeResult result =
      grade(shift_request({"DOUT"}), {{compiled_net("DIN"), 0}});
  // 12 periods, one sample each (CLK low, before the period's edge). DOUT shows
  // DIN three edges later; the reset holds it at 0 through the first two.
  std::vector<std::vector<bool>> expected;
  for (const int dout : {0, 0, 0, 0, 0, 1, 0, 1, 1, 0, 0, 1}) {
    expected.push_back({dout != 0});
  }
  REQUIRE(result.golden == expected);
  REQUIRE(result.first_sample == std::vector<int32_t>{5});  // DIN SA0: DOUT's first 1
}

TEST_CASE("Sequence grading credits only observed outputs at sampled cycles",
          "[sequence_grade]") {
  const std::vector<SequenceFaultSpec> extra = {{compiled_net("EXTRA"), 0},
                                                {compiled_net("EXTRA"), 1}};
  const SequenceGradeResult unobserved = grade(shift_request({"DOUT"}), extra);
  REQUIRE(unobserved.first_sample == std::vector<int32_t>{-1, -1});
  const SequenceGradeResult observed = grade(shift_request({"EXTRA"}), extra);
  REQUIRE(observed.first_sample[0] >= 0);
  REQUIRE(observed.first_sample[1] >= 0);

  SequenceGradeRequest unsampled = shift_request({"DOUT", "EXTRA"});
  unsampled.sample.assign(unsampled.sample.size(), false);
  const SequenceGradeResult none = grade(unsampled, every_fault());
  REQUIRE(none.golden.empty());
  for (const int32_t first : none.first_sample) {
    REQUIRE(first == -1);
  }
}

TEST_CASE("Sequence grading: thread count does not change the result",
          "[sequence_grade]") {
  // Eight copies of every fault: several 63-fault batches to spread.
  std::vector<SequenceFaultSpec> faults;
  for (int copy = 0; copy < 8; ++copy) {
    for (const SequenceFaultSpec& spec : every_fault()) {
      faults.push_back(spec);
    }
  }
  REQUIRE(faults.size() > 2 * static_cast<size_t>(kBatchSize));
  const SequenceGradeResult one = grade(shift_request({"DOUT", "EXTRA"}), faults, 1);
  const SequenceGradeResult four = grade(shift_request({"DOUT", "EXTRA"}), faults, 4);
  REQUIRE(one.first_sample == four.first_sample);
  REQUIRE(one.golden == four.golden);
}

TEST_CASE("Sequence grading: early exit does not change the result",
          "[sequence_grade]") {
  const CachedGraph& graph =
      load_cached_graph(test::fixture_path(kFixture), test::cell_map_path(), "fail");
  const SequenceGradeRequest request = shift_request({"DOUT"});
  TestVector vec;
  const int din = graph.parsed.net_id_by_name("DIN");
  const int clk = graph.parsed.net_id_by_name("CLK");
  const int reset_b = graph.parsed.net_id_by_name("RESET_B");
  for (size_t c = 0; c < request.cycles.size(); ++c) {
    TestCycle cycle;
    cycle.inputs = {{clk, request.cycles[c][0]},
                    {reset_b, request.cycles[c][1]},
                    {din, request.cycles[c][2]}};
    cycle.sample_outputs = request.sample[c];
    vec.cycles.push_back(cycle);
  }
  FaultBatch batch;
  const std::vector<SequenceFaultSpec> faults = every_fault();
  for (int lane = 1; lane <= kBatchSize && lane <= static_cast<int>(faults.size());
       ++lane) {
    CompactFault fault;
    fault.net_index = faults[static_cast<size_t>(lane - 1)].compiled_net_index;
    fault.type = faults[static_cast<size_t>(lane - 1)].fault_type == 0 ? FaultType::SA0
                                                                         : FaultType::SA1;
    fault.bit = static_cast<uint8_t>(lane);
    fault.sa_mask = 1ULL << lane;
    batch.faults[lane - 1] = fault;
    batch.mask |= fault.sa_mask;
    batch.size = lane;
  }
  const std::vector<uint32_t> observe = {compiled_net("DOUT")};
  std::array<int32_t, 64> early{};
  std::array<int32_t, 64> full{};
  const BitParallelSim sim;
  const uint64_t early_mask = sim.simulate_batch_observed(graph.cg, vec, batch, observe,
                                                          early, true);
  const uint64_t full_mask =
      sim.simulate_batch_observed(graph.cg, vec, batch, observe, full, false);
  REQUIRE(early_mask == full_mask);
  REQUIRE(early == full);
}

TEST_CASE("Sequence grading under a held reset ignores the flops' initial state",
          "[sequence_grade]") {
  // Outside the reset tree (RESET_B and its fanout branches), a fault can't keep
  // a flop out of reset, so the held reset fixes every lane's state.
  const CachedGraph& graph =
      load_cached_graph(test::fixture_path(kFixture), test::cell_map_path(), "fail");
  const uint32_t reset = compiled_net("RESET_B");
  std::vector<uint32_t> reset_tree = {reset};
  for (const SimNode& node : graph.cg.nodes) {
    if (node.type == GateType::BUF && node.in0 == reset) {
      reset_tree.push_back(node.out);
    }
  }
  REQUIRE(reset_tree.size() == 4);  // the stem and one branch per flop
  std::vector<SequenceFaultSpec> faults;
  for (const SequenceFaultSpec& spec : every_fault()) {
    if (std::find(reset_tree.begin(), reset_tree.end(), spec.compiled_net_index) ==
        reset_tree.end()) {
      faults.push_back(spec);
    }
  }
  SequenceGradeRequest zero = shift_request({"DOUT", "EXTRA"}, /*reset_held=*/true);
  SequenceGradeRequest one = zero;
  one.initial_ff_value = true;
  const SequenceGradeResult from_zero = grade(zero, faults);
  const SequenceGradeResult from_one = grade(one, faults);
  REQUIRE(from_zero.golden == from_one.golden);
  REQUIRE(from_zero.first_sample == from_one.first_sample);

  // RESET_B stuck inactive leaves the faulty machine unreset: its initial state
  // shows (at EXTRA, sample 0 from all 1s; only once DIN's first 1 arrives from
  // all 0s). A two-valued grade of such a fault depends on that assumption.
  const std::vector<SequenceFaultSpec> stuck_inactive = {{reset, 1}};
  REQUIRE(grade(zero, stuck_inactive).first_sample == std::vector<int32_t>{2});
  REQUIRE(grade(one, stuck_inactive).first_sample == std::vector<int32_t>{0});

  // Without the reset the initial state shows: the first sample reads it at EXTRA.
  SequenceGradeRequest free_zero = shift_request({"EXTRA"});
  free_zero.cycles[0][1] = true;
  SequenceGradeRequest free_one = free_zero;
  free_one.initial_ff_value = true;
  REQUIRE(grade(free_zero, {}).golden.front() != grade(free_one, {}).golden.front());
}

TEST_CASE("Sequence grading injects a fault only in its active cycles",
          "[sequence_grade]") {
  const std::vector<SequenceFaultSpec> din_sa0 = {{compiled_net("DIN"), 0}};
  const SequenceGradeRequest request = shift_request({"DOUT"});
  // Period k is cycles 2k (CLK low) and 2k+1 (its edge). The reset holds the
  // first two; DIN's first 1 is latched at period 2's edge and reaches DOUT at
  // sample 5, and DIN is 0 in period 3.
  const auto active_in = [&](const std::vector<size_t>& periods) {
    SequenceGradeRequest masked = request;
    masked.fault_active.assign(masked.cycles.size(), false);
    for (size_t period : periods) {
      masked.fault_active[2 * period] = true;
      masked.fault_active[2 * period + 1] = true;
    }
    return masked;
  };
  REQUIRE(grade(active_in({}), din_sa0).first_sample == std::vector<int32_t>{-1});
  REQUIRE(grade(active_in({2}), din_sa0).first_sample == std::vector<int32_t>{5});
  REQUIRE(grade(active_in({2}), din_sa0, 1, true).first_sample ==
          std::vector<int32_t>{5});
  REQUIRE(grade(active_in({3}), din_sa0).first_sample == std::vector<int32_t>{-1});

  SequenceGradeRequest every = request;
  every.fault_active.assign(request.cycles.size(), true);
  REQUIRE(grade(every, every_fault()).first_sample ==
          grade(request, every_fault()).first_sample);

  SequenceGradeRequest short_mask = request;
  short_mask.fault_active.assign(3, true);
  REQUIRE_THROWS_AS(grade(short_mask, din_sa0), std::runtime_error);
}
