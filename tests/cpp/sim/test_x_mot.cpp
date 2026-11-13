#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <random>
#include <string>
#include <vector>

#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/xsim/golden_ref_sim_x.hpp"
#include "sim/xsim/sequence_grade_x.hpp"
#include "sim/xsim/sequence_mot_x.hpp"

using namespace faultflow;

namespace {

const CachedGraph& load(const std::string& fixture, const std::string& policy) {
  return load_cached_graph(test::fixture_path(fixture), test::cell_map_path(), policy);
}

std::vector<std::string> ports(const ParsedGraph& parsed, const std::string& direction) {
  std::vector<std::string> names;
  for (const auto& [name, port] : parsed.top_module().ports) {
    if (port.direction == direction) {
      names.push_back(name);
    }
  }
  return names;
}

std::vector<SequenceFaultSpec> every_fault(const CompiledSimGraph& cg) {
  std::vector<SequenceFaultSpec> faults;
  for (uint32_t net = 0; net < static_cast<uint32_t>(cg.net_count); ++net) {
    faults.push_back({net, 0});
    faults.push_back({net, 1});
  }
  return faults;
}

XMotResult mot(const std::string& fixture, const XMotRequest& request,
               const std::string& policy = "fail", int threads = 1) {
  return grade_sequence_faults_mot_x(test::fixture_path(fixture), test::cell_map_path(),
                                     request, policy, {}, threads);
}

XSequenceGradeResult single(const std::string& fixture, const XMotRequest& request,
                            const std::string& policy = "fail") {
  return grade_sequence_faults_x(test::fixture_path(fixture), test::cell_map_path(),
                                 request.sequence, policy, {}, 1, false);
}

bool compared(const XMotRequest& request, size_t sample, size_t output) {
  return request.compare.empty() || request.compare[sample][output];
}

// Random values on every input each cycle (clocks too), a random half of the
// cycles sampled, a random 70% of the outputs compared at each sample, the fault
// kept out of a random cycle in eight.
XMotRequest random_request(const CachedGraph& g, std::mt19937& rng, int cycles) {
  XMotRequest request;
  SequenceGradeRequest& base = request.sequence.base;
  base.input_order = ports(g.parsed, "input");
  base.observe_outputs = ports(g.parsed, "output");
  for (int c = 0; c < cycles; ++c) {
    std::vector<bool> row;
    for (size_t i = 0; i < base.input_order.size(); ++i) {
      row.push_back(rng() % 2 == 0);
    }
    base.cycles.push_back(row);
    base.fault_active.push_back(rng() % 8 != 0);
    const bool sampled = rng() % 2 == 0 || c + 1 == cycles;
    base.sample.push_back(sampled);
    if (sampled) {
      std::vector<bool> care;
      for (size_t o = 0; o < base.observe_outputs.size(); ++o) {
        care.push_back(rng() % 10 < 7);
      }
      request.compare.push_back(care);
    }
  }
  base.faults = every_fault(g.cg);
  request.sequence.initial_unknown = true;
  request.seconds_per_fault = 0;
  request.witness = true;
  return request;
}

// Per fault, per power-up state of the flops (bit i of the state = ff_nodes[i]),
// the first sample at which a compared bit the good machine knows differs from
// it: -1 where that state escapes. Every state enumerated; no latch, x bit or
// unknown input in these fixtures and requests. `one_sample`: per fault, does
// one sample catch every state.
std::vector<std::vector<int32_t>> every_power_up(const CachedGraph& g,
                                                 const XMotRequest& request,
                                                 const std::vector<std::vector<Logic>>& golden,
                                                 std::vector<bool>& one_sample) {
  const CompiledSimGraph& cg = g.cg;
  XRunOptions options;
  const TestVector vec = sequence_vector_x(g.parsed, cg, request.sequence, options);
  options.initial_state = Logic::ZERO;
  std::vector<int> yids;
  std::vector<uint32_t> compiled;
  sequence_observed(g.parsed, cg, request.sequence.base.observe_outputs, yids, compiled);
  const size_t flops = cg.ff_nodes.size();
  REQUIRE(flops <= 10);
  const auto& specs = request.sequence.base.faults;
  std::vector<std::vector<int32_t>> first(specs.size());
  // every_state[k][s]: sample s catches fault k from every state so far.
  std::vector<std::vector<bool>> every_state(specs.size(),
                                             std::vector<bool>(golden.size(), true));
  const GoldenRefSimX ref;
  for (uint32_t state = 0; state < (1u << flops); ++state) {
    TestVector v = vec;
    for (uint32_t i = 0; i < flops; ++i) {
      v.initial_ff_state[i] = ((state >> i) & 1u) != 0;
    }
    for (size_t k = 0; k < specs.size(); ++k) {
      const CompactFault fault = sequence_fault(specs[k], cg);
      const auto faulty = ref.simulate_sequence(cg, v, &fault, options, &yids);
      int32_t at = -1;
      for (size_t s = 0; s < golden.size(); ++s) {
        bool caught = false;
        for (size_t o = 0; o < yids.size(); ++o) {
          const Logic f = faulty[s].at(yids[o]);
          REQUIRE(is_known(f));
          caught = caught || (compared(request, s, o) && is_known(golden[s][o]) &&
                              f != golden[s][o]);
        }
        if (caught && at < 0) {
          at = static_cast<int32_t>(s);
        }
        every_state[k][s] = every_state[k][s] && caught;
      }
      first[k].push_back(at);
    }
  }
  one_sample.clear();
  for (const auto& row : every_state) {
    one_sample.push_back(std::any_of(row.begin(), row.end(), [](bool b) { return b; }));
  }
  return first;
}

uint32_t net(const CachedGraph& g, const std::string& name) {
  return static_cast<uint32_t>(g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name(name)));
}

size_t flop_of(const CachedGraph& g, const std::string& q_net) {
  const uint32_t q = net(g, q_net);
  for (size_t i = 0; i < g.cg.ff_nodes.size(); ++i) {
    if (g.cg.nodes[g.cg.ff_nodes[i]].out == q) {
      return i;
    }
  }
  FAIL("no flop drives " << q_net);
  return 0;
}

// tiny_mot: two cycles per value of A (CLK low, sampled; CLK high), SEL and M
// as given, RESET_B low in the first `reset_periods` periods.
XMotRequest held(const std::vector<int>& a_values, bool sel, bool m,
                 const std::vector<std::string>& observe, int reset_periods = 0) {
  XMotRequest request;
  SequenceGradeRequest& base = request.sequence.base;
  base.input_order = {"CLK", "RESET_B", "A", "SEL", "M"};
  base.observe_outputs = observe;
  int period = 0;
  for (const int a : a_values) {
    const bool reset_b = period++ >= reset_periods;
    base.cycles.push_back({false, reset_b, a != 0, sel, m});
    base.sample.push_back(true);
    base.cycles.push_back({true, reset_b, a != 0, sel, m});
    base.sample.push_back(false);
  }
  request.sequence.initial_unknown = true;
  request.seconds_per_fault = 0;
  request.witness = true;
  return request;
}

const std::string kMot = "tiny_mot.json";

}  // namespace

TEST_CASE("Multiple observation detects exactly where every power-up state is caught",
          "[xsim][xmot]") {
  std::mt19937 rng(20261113);
  int multiple = 0;  // detected, but no single sample catches every state
  for (const std::string fixture :
       {"tiny_mot.json", "tiny_seq_observe.json", "tiny_dff_fanout.json", "tiny_sdfrtp.json",
        "tiny_dffsr.json", "tiny_edfxtp.json", "tiny_dff_neg.json", "tiny_sync_reset.json",
        "tiny_tied_ff_pins.json"}) {
    INFO(fixture);
    const CachedGraph& g = load(fixture, "fail");
    for (int round = 0; round < 3; ++round) {
      const XMotRequest request = random_request(g, rng, 14);
      const XMotResult result = mot(fixture, request);
      REQUIRE(result.golden == single(fixture, request).golden);
      std::vector<bool> one_sample;
      const auto first = every_power_up(g, request, result.golden, one_sample);
      for (size_t k = 0; k < first.size(); ++k) {
        INFO("fault " << k);
        const bool all = std::all_of(first[k].begin(), first[k].end(),
                                     [](int32_t at) { return at >= 0; });
        if (all) {
          multiple += one_sample[k] ? 0 : 1;
          REQUIRE(result.outcome[k] == MotOutcome::DETECTED);
          REQUIRE(result.detected_by[k] == *std::max_element(first[k].begin(), first[k].end()));
          REQUIRE(result.witness[k].empty());
        } else {
          REQUIRE(result.outcome[k] == MotOutcome::ESCAPES);
          REQUIRE(result.detected_by[k] == -1);
          const auto& w = result.witness[k];
          REQUIRE(w.size() == g.cg.ff_nodes.size());
          uint32_t state = 0;
          for (size_t i = 0; i < w.size(); ++i) {
            REQUIRE(w[i] >= 0);
            state |= static_cast<uint32_t>(w[i]) << i;
          }
          REQUIRE(first[k][state] == -1);
        }
      }
    }
  }
  // The random programs do reach faults only multiple observation detects.
  REQUIRE(multiple > 0);
}

TEST_CASE("Multiple observation keeps the single-sample X grade's detections and adds to them",
          "[xsim][xmot]") {
  std::mt19937 rng(7);
  for (const auto& [fixture, policy] : std::vector<std::pair<std::string, std::string>>{
           {"tiny_mot.json", "fail"},
           {"tiny_seq_observe.json", "fail"},
           {"tiny_icg.json", "fail"},
           {"tiny_icg_fanout.json", "fail"},
           {"tiny_xlatch.json", "fail+x"},
           {"tiny_latch.json", "fail+x"},
           {"tiny_xbit.json", "fail+x"},
           {"tiny_tied_ff_pins.json", "fail"}}) {
    INFO(fixture);
    const CachedGraph& g = load(fixture, policy);
    for (int round = 0; round < 3; ++round) {
      XMotRequest request = random_request(g, rng, 16);
      request.compare.clear();  // grade_sequence_faults_x compares every output
      for (size_t c = 0; c < request.sequence.base.cycles.size(); ++c) {
        request.sequence.unknown.push_back({});
        for (size_t i = 0; i < request.sequence.base.input_order.size(); ++i) {
          request.sequence.unknown.back().push_back(rng() % 9 == 0);
        }
      }
      const XSequenceGradeResult one = single(fixture, request, policy);
      const XMotResult result = mot(fixture, request, policy);
      const XMotResult threaded = mot(fixture, request, policy, 3);
      REQUIRE(result.golden == one.golden);
      REQUIRE(threaded.outcome == result.outcome);
      REQUIRE(threaded.detected_by == result.detected_by);
      for (size_t k = 0; k < one.first_definite.size(); ++k) {
        INFO("fault " << k);
        REQUIRE(result.outcome[k] != MotOutcome::UNDECIDED);
        if (one.first_definite[k] >= 0) {
          REQUIRE(result.outcome[k] == MotOutcome::DETECTED);
          REQUIRE(result.detected_by[k] <= one.first_definite[k]);
        } else if (one.first_possible[k] < 0) {
          REQUIRE(result.outcome[k] == MotOutcome::ESCAPES);
        }
      }
    }
  }
}

TEST_CASE("A fault caught from one power-up state early and from the other later is detected",
          "[xsim][xmot]") {
  const CachedGraph& g = load(kMot, "fail");
  // SEL stuck at 0: Y shows h0, held at its power-up value, where A is expected.
  XMotRequest request = held({0, 0, 1, 0}, true, false, {"Y"});
  request.sequence.base.faults = {{net(g, "SEL"), 0}};
  const XSequenceGradeResult one = single(kMot, request);
  REQUIRE(one.first_definite[0] == -1);
  REQUIRE(one.first_possible[0] == 0);
  const XMotResult result = mot(kMot, request);
  REQUIRE(result.outcome[0] == MotOutcome::DETECTED);
  REQUIRE(result.detected_by[0] == 2);  // h0 = 1 fails at sample 0, h0 = 0 at 2

  // With A 0 throughout, h0 = 0 never fails.
  XMotRequest zeros = held({0, 0, 0}, true, false, {"Y"});
  zeros.sequence.base.faults = request.sequence.base.faults;
  const XMotResult escapes = mot(kMot, zeros);
  REQUIRE(escapes.outcome[0] == MotOutcome::ESCAPES);
  REQUIRE(escapes.witness[0][flop_of(g, "h0q")] == 0);
  REQUIRE(escapes.witness[0][flop_of(g, "h1q")] >= 0);
}

TEST_CASE("Correlated unknowns: a flop XNOR itself is 1, h0 ^ h1 is caught by a 0 and a 1",
          "[xsim][xmot]") {
  const CachedGraph& g = load(kMot, "fail");
  // M stuck at 1: Z shows h0 XNOR buf(h0), 1 from every state, where 0 is expected.
  XMotRequest z = held({0, 1}, false, false, {"Z"});
  z.sequence.base.faults = {{net(g, "M"), 1}};
  REQUIRE(single(kMot, z).first_definite[0] == -1);
  const XMotResult zr = mot(kMot, z);
  REQUIRE(zr.outcome[0] == MotOutcome::DETECTED);
  REQUIRE(zr.detected_by[0] == 0);

  // M stuck at 1: W shows h0 ^ h1 where r is expected. r is cleared in period 0
  // and captures A = 1 at period 1's edge: 0 at samples 0-1, then 1.
  XMotRequest w = held({0, 1, 1}, false, false, {"W"}, 1);
  w.sequence.base.faults = {{net(g, "M"), 1}};
  const XMotResult wr = mot(kMot, w);
  REQUIRE(wr.outcome[0] == MotOutcome::DETECTED);
  REQUIRE(wr.detected_by[0] == 2);
  // r 0 throughout: h0 == h1 escapes.
  XMotRequest w0 = held({0, 0, 0}, false, false, {"W"}, 1);
  w0.sequence.base.faults = w.sequence.base.faults;
  const XMotResult w0r = mot(kMot, w0);
  REQUIRE(w0r.outcome[0] == MotOutcome::ESCAPES);
  REQUIRE(w0r.witness[0][flop_of(g, "h0q")] == w0r.witness[0][flop_of(g, "h1q")]);
}

TEST_CASE("A stuck reset on a design initialised only by reset escapes",
          "[xsim][xmot]") {
  const CachedGraph& g = load("tiny_seq_observe.json", "fail");
  XMotRequest request;
  SequenceGradeRequest& base = request.sequence.base;
  base.input_order = {"CLK", "RESET_B", "DIN"};
  base.observe_outputs = {"DOUT", "EXTRA"};
  int period = 0;
  for (const int din : {0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0}) {
    const bool reset_b = period++ >= 2;
    base.cycles.push_back({false, reset_b, din != 0});
    base.sample.push_back(true);
    base.cycles.push_back({true, reset_b, din != 0});
    base.sample.push_back(false);
  }
  // RESET_B stuck inactive, DIN unknown throughout: the faulty chip may power up
  // in the reset state and see the good machine's DIN.
  base.faults = {{net(g, "RESET_B"), 1}};
  for (size_t c = 0; c < base.cycles.size(); ++c) {
    request.sequence.unknown.push_back({false, false, true});
  }
  request.seconds_per_fault = 0;
  const XMotResult result = mot("tiny_seq_observe.json", request);
  REQUIRE(result.outcome[0] == MotOutcome::ESCAPES);
}

TEST_CASE("A loop or a bus in the graph, or too many variables, leaves a fault undecided",
          "[xsim][xmot]") {
  for (const std::string fixture : {"tiny_loop_sr.json", "tiny_tbus.json"}) {
    INFO(fixture);
    const CachedGraph& g = load(fixture, "fail+x");
    std::mt19937 rng(3);
    XMotRequest request = random_request(g, rng, 6);
    const XMotResult result = mot(fixture, request, "fail+x");
    REQUIRE(std::all_of(result.outcome.begin(), result.outcome.end(),
                        [](MotOutcome o) { return o == MotOutcome::UNDECIDED; }));
  }
  const CachedGraph& g = load(kMot, "fail");
  XMotRequest request = held({0, 0, 1, 0}, true, false, {"Y"});
  request.sequence.base.faults = {{net(g, "SEL"), 0}};
  request.max_variables = 2;  // the three flops' power-up values alone need three
  REQUIRE(mot(kMot, request).outcome[0] == MotOutcome::UNDECIDED);
}
