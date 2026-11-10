#include <catch2/catch_test_macros.hpp>

#include <random>
#include <string>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/engine/sequence_grade.hpp"
#include "sim/xsim/sequence_grade_x.hpp"

using namespace faultflow;

namespace {

// tiny_seq_observe: DIN -> u0 -> u1 -> u2 -> DOUT (dfrtp, RESET_B), EXTRA = ~u0.Q.
const std::string kFixture = "tiny_seq_observe.json";

// Two cycles per clock period, CLK low (sampled) then high; RESET_B low for the
// periods `reset_periods` says.
SequenceGradeRequest shift_request(int reset_periods) {
  SequenceGradeRequest request;
  request.input_order = {"CLK", "RESET_B", "DIN"};
  request.observe_outputs = {"DOUT", "EXTRA"};
  int period = 0;
  for (const int din : {0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0}) {
    const bool reset_b = period++ >= reset_periods;
    request.cycles.push_back({false, reset_b, din != 0});
    request.sample.push_back(true);
    request.cycles.push_back({true, reset_b, din != 0});
    request.sample.push_back(false);
  }
  return request;
}

const CachedGraph& graph() {
  return load_cached_graph(test::fixture_path(kFixture), test::cell_map_path(), "fail");
}

std::vector<SequenceFaultSpec> every_fault() {
  std::vector<SequenceFaultSpec> faults;
  for (uint32_t net = 0; net < static_cast<uint32_t>(graph().cg.net_count); ++net) {
    faults.push_back({net, 0});
    faults.push_back({net, 1});
  }
  return faults;
}

uint32_t net_of(const std::string& name) {
  return static_cast<uint32_t>(
      graph().cg.yosys_to_compiled.at(graph().parsed.net_id_by_name(name)));
}

XSequenceGradeResult grade_x(const XSequenceGradeRequest& request, int threads = 1,
                             bool reference = false) {
  return grade_sequence_faults_x(test::fixture_path(kFixture), test::cell_map_path(),
                                 request, "fail", {}, threads, reference);
}

}  // namespace

TEST_CASE("With nothing unknown the X grader detects exactly what the two-valued one does",
          "[xsim][sequence_grade_x]") {
  for (const bool ones : {false, true}) {
    XSequenceGradeRequest request;
    request.base = shift_request(2);
    request.base.faults = every_fault();
    request.base.initial_ff_value = ones;
    request.initial_unknown = false;
    const SequenceGradeResult two =
        grade_sequence_faults(test::fixture_path(kFixture), test::cell_map_path(),
                              request.base, "fail", {}, 1, false);
    const XSequenceGradeResult x = grade_x(request);
    REQUIRE(x.first_definite == two.first_sample);
    REQUIRE(x.first_possible == std::vector<int32_t>(two.first_sample.size(), -1));
    REQUIRE(x.golden.size() == two.golden.size());
    for (size_t s = 0; s < two.golden.size(); ++s) {
      for (size_t o = 0; o < two.golden[s].size(); ++o) {
        REQUIRE(x.golden[s][o] == logic_of(two.golden[s][o]));
      }
    }
  }
}

TEST_CASE("The X grader's bit-parallel and reference runs agree, any thread count",
          "[xsim][sequence_grade_x]") {
  std::mt19937 rng(42);
  for (int round = 0; round < 6; ++round) {
    XSequenceGradeRequest request;
    request.base = shift_request(round % 3);  // no reset, a short one, or two periods
    request.base.faults = every_fault();
    request.initial_unknown = true;
    for (size_t c = 0; c < request.base.cycles.size(); ++c) {
      request.unknown.push_back({false, rng() % 7 == 0, rng() % 3 == 0});
    }
    const XSequenceGradeResult ref = grade_x(request, 1, true);
    for (const int threads : {1, 3}) {
      const XSequenceGradeResult par = grade_x(request, threads, false);
      REQUIRE(par.golden == ref.golden);
      REQUIRE(par.first_definite == ref.first_definite);
      REQUIRE(par.first_possible == ref.first_possible);
    }
  }
}

TEST_CASE("Powered up unknown, a flop's output is X until data has shifted through",
          "[xsim][sequence_grade_x]") {
  XSequenceGradeRequest request;
  request.base = shift_request(0);  // no reset at all
  request.base.observe_outputs = {"DOUT"};
  request.base.faults = {{net_of("DIN"), 0}};
  const XSequenceGradeResult x = grade_x(request);
  // DOUT is three flops from DIN: unknown for the samples before three edges.
  REQUIRE(x.golden[0][0] == Logic::X);
  REQUIRE(x.golden[2][0] == Logic::X);
  REQUIRE(x.golden[3][0] == Logic::ZERO);
  // The first DIN 1 is latched at period 2's edge and reaches DOUT at sample 5.
  REQUIRE(x.golden[5][0] == Logic::ONE);
  REQUIRE(x.first_definite[0] == 5);
}

TEST_CASE("A stuck reset on a design initialised only by reset is possible, never definite",
          "[xsim][sequence_grade_x]") {
  XSequenceGradeRequest request;
  request.base = shift_request(2);
  // RESET_B stuck inactive: its flops never initialise in the faulty machine.
  request.base.faults = {{net_of("RESET_B"), 1}};
  // DIN unknown throughout, so only the reset makes the good machine known.
  for (size_t c = 0; c < request.base.cycles.size(); ++c) {
    request.unknown.push_back({false, false, true});
  }
  const XSequenceGradeResult x = grade_x(request);
  REQUIRE(x.golden[0][0] == Logic::ZERO);  // cleared in cycle 0, before its sample
  // The unknown DIN reaches DOUT three edges after the reset ends (periods 2-4).
  REQUIRE(x.golden[4][0] == Logic::ZERO);
  REQUIRE(x.golden[5][0] == Logic::X);
  REQUIRE(x.first_definite[0] == -1);
  REQUIRE(x.first_possible[0] == 0);
}
