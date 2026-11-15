// Stage 6 of the X engine: the scan protocol on BitParallelSimX (every flop
// powered up X, an unknown blackbox's outputs X in every cycle), checked lane by
// lane against GoldenRefSimX; and a scan ATPG view's tie marked unknown, which
// only an X-mode graph accepts.
//
// tiny_scan_x: a chain ff0 -> ff1 -> ff2; ff0 captures dout & D1 (u_mem's
// unknown output, blocked by D1 = 0), ff1 captures D2, ff2 ff0's state. P =
// dout | Q1 (blocked by Q1 = 1); R = nsq ^ D0, nsq a non-scan flop capturing D2
// on every clock -- unknown at power-up, known after the first pulse.
#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "common/errors.hpp"
#include "helpers/test_helpers.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "scan/scan_pattern_sim.hpp"
#include "sim/xsim/logic.hpp"

using namespace faultflow;
using namespace faultflow::scan;

namespace {

struct Bits {
  uint32_t state;
  bool next() {
    state = state * 1664525u + 1013904223u;
    return (state >> 16) & 1u;
  }
};

const std::vector<std::string> kBlackbox{"u_mem"};
const std::vector<int> kUnknown{11};  // u_mem.dout

ScanPatternRequest x_pattern(Bits& bits) {
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out_0"};
  request.functional_output_ports = {"Q0", "P", "R"};
  request.max_chain_length = 3;
  std::vector<bool> load;
  for (int i = 0; i < 3; ++i) {
    load.push_back(bits.next());
  }
  request.load_seqs[0] = load;
  for (const char* pi : {"D0", "D1", "D2"}) {
    request.capture_pi_values[pi] = bits.next();
  }
  return request;
}

// Pattern p's launch: none (stuck-at), launch on capture, or launch on shift
// (a random launch-shift scan-in bit).
void set_launch(ScanPatternRequest& request, int p, Bits& bits) {
  request.loc_two_capture = p % 3 == 1;
  request.los_two_capture = p % 3 == 2;
  if (request.los_two_capture) {
    request.los_launch_scan_in[0] = bits.next();
  }
}

std::vector<ScanProtocolFaultSpec> every_fault(const CompiledSimGraph& cg) {
  std::vector<ScanProtocolFaultSpec> faults;
  // Three copies: more than one 63-lane batch, a fault in several lanes.
  for (int copy = 0; copy < 3; ++copy) {
    for (uint32_t net = 0; net < static_cast<uint32_t>(cg.net_count); ++net) {
      faults.push_back({net, 0});
      faults.push_back({net, 1});
    }
  }
  return faults;
}

}  // namespace

TEST_CASE("X scan protocol grade == scalar X oracle, every fault",
          "[scan][xsim][reference]") {
  const std::string json = test::fixture_path("tiny_scan_x.json");
  const CachedGraph& graph =
      load_cached_graph(json, test::cell_map_path(), "fail+x", kBlackbox);
  const auto faults = every_fault(graph.cg);
  Bits bits{7u};
  size_t definite = 0;
  size_t possible = 0;
  size_t launched = 0;  // a transition's definite or possible detections
  for (int p = 0; p < 24; ++p) {
    ScanProtocolFaultRequest request;
    request.pattern = x_pattern(bits);
    set_launch(request.pattern, p, bits);
    request.faults = faults;
    request.capture_diffs = (p % 2) == 0;
    const auto graded = simulate_scan_protocol_faults_x(
        json, test::cell_map_path(), request, "fail+x", 2, kBlackbox, kUnknown);
    const auto reference = simulate_scan_protocol_faults_x_reference(
        json, test::cell_map_path(), request, "fail+x", kBlackbox, kUnknown);
    REQUIRE(graded.lanes.size() == faults.size());
    REQUIRE(reference.size() == faults.size());
    for (size_t i = 0; i < faults.size(); ++i) {
      INFO("pattern " << p << " fault " << i << " net "
                      << faults[i].compiled_net_index << " sa"
                      << int(faults[i].fault_type));
      REQUIRE(graded.lanes[i].fault_index == i);
      REQUIRE(graded.lanes[i].outcome == reference[i]);
      definite += reference[i] == XDetection::DEFINITE;
      possible += reference[i] == XDetection::POSSIBLE;
      launched += p % 3 != 0 && reference[i] != XDetection::NONE;
    }
  }
  REQUIRE(definite > 0);
  REQUIRE(possible > 0);
  REQUIRE(launched > 0);
}

TEST_CASE("X scan protocol: an unknown value a controlling input blocks is known",
          "[scan][xsim]") {
  const std::string json = test::fixture_path("tiny_scan_x.json");
  ScanPatternRequest request;
  request.clock_ports = {"CLK"};
  request.scan_enable_port = "scan_en";
  request.scan_input_ports = {"scan_in"};
  request.scan_output_ports = {"scan_out_0"};
  request.functional_output_ports = {"Q0", "P", "R"};
  request.max_chain_length = 3;
  // Loaded in shift order: ff2, ff1, ff0 -- Q1 (ff1) loaded 1.
  request.load_seqs[0] = {false, true, false};
  request.capture_pi_values = {{"D0", false}, {"D1", false}, {"D2", true}};
  const ScanPatternResultX blocked = simulate_scan_pattern_x(
      json, test::cell_map_path(), request, "fail+x", kBlackbox, kUnknown);
  // Functional outputs, sampled before the capture: P = dout | Q1 = 1 with
  // Q1 = 1; R = nsq ^ D0, nsq clocked with D2 = 1 through the load: 1.
  REQUIRE(blocked.real_po_values.at("P") == Logic::ONE);
  REQUIRE(blocked.real_po_values.at("R") == Logic::ONE);
  // Unloaded last-first: ff2, ff1, ff0. ff0 captured dout & D1 = 0: known.
  REQUIRE(blocked.unload_seqs.at(0).at(2) == Logic::ZERO);

  request.capture_pi_values["D1"] = true;  // ff0 now captures dout
  request.load_seqs[0] = {false, false, false};  // and P = dout
  const ScanPatternResultX open = simulate_scan_pattern_x(
      json, test::cell_map_path(), request, "fail+x", kBlackbox, kUnknown);
  REQUIRE(open.real_po_values.at("P") == Logic::X);
  REQUIRE(open.unload_seqs.at(0).at(2) == Logic::X);
  // The two-valued protocol reads the undriven output as 0 instead.
  const ScanPatternResult two = simulate_scan_pattern(
      json, test::cell_map_path(), request, "fail", kBlackbox);
  REQUIRE_FALSE(two.real_po_values.at("P"));
  REQUIRE_FALSE(two.unload_seqs.at(0).at(2));
}

TEST_CASE("X scan protocol with nothing unknown equals the two-valued one",
          "[scan][xsim]") {
  // Every flop of tiny_scan_chain is scanned, so the load overwrites its power-up
  // X; nothing else is unknown.
  const std::string json = test::fixture_path("tiny_scan_chain.json");
  const CachedGraph& graph = load_cached_graph(json, test::cell_map_path(), "fail+x");
  const auto faults = every_fault(graph.cg);
  Bits bits{11u};
  for (int p = 0; p < 12; ++p) {
    ScanProtocolFaultRequest request;
    request.pattern.clock_ports = {"CLK"};
    request.pattern.scan_enable_port = "scan_en";
    request.pattern.scan_input_ports = {"scan_in"};
    request.pattern.scan_output_ports = {"scan_out_0"};
    request.pattern.functional_output_ports = {"Q0", "Q1"};
    request.pattern.max_chain_length = 3;
    request.pattern.load_seqs[0] = {bits.next(), bits.next(), bits.next()};
    for (const char* pi : {"D0", "D1", "D2"}) {
      request.pattern.capture_pi_values[pi] = bits.next();
    }
    set_launch(request.pattern, p, bits);
    request.faults = faults;
    request.capture_diffs = true;
    const auto x = simulate_scan_protocol_faults_x(json, test::cell_map_path(),
                                                   request, "fail+x", 1, {}, {});
    const auto two =
        simulate_scan_protocol_faults(json, test::cell_map_path(), request, "fail");
    for (const auto& [chain, bits_x] : x.golden.unload_seqs) {
      const auto& bits_two = two.golden.unload_seqs.at(chain);
      REQUIRE(bits_x.size() == bits_two.size());
      for (size_t t = 0; t < bits_x.size(); ++t) {
        REQUIRE(bits_x[t] == logic_of(bits_two[t]));
      }
    }
    for (const auto& batch : two.batches) {
      for (const auto& lane : batch.lanes) {
        INFO("pattern " << p << " fault " << lane.fault_index);
        const auto& lx = x.lanes.at(lane.fault_index);
        REQUIRE(lx.outcome == (lane.outcome == ScanProtocolFaultOutcome::PASS
                                   ? XDetection::DEFINITE
                                   : XDetection::NONE));
        REQUIRE(lx.diff_unload_seqs == lane.diff_unload_seqs);
      }
    }
  }
}

TEST_CASE("X scan protocol: no transition credit where the launch leaves it unknown",
          "[scan][xsim][xtransition]") {
  // Launch on capture: ff0 captures dout & D1. With D1 = 1 its state after the
  // launch is u_mem's unknown output, so Q0's fall from a loaded 1 may never
  // happen: no credit, though the faulty Q0 = 1 shows at the Q0 output. With
  // D1 = 0 ff0 captures a known 0 and the fall is a definite detection.
  const std::string json = test::fixture_path("tiny_scan_x.json");
  const CachedGraph& graph =
      load_cached_graph(json, test::cell_map_path(), "fail+x", kBlackbox);
  const auto q0 = static_cast<uint32_t>(
      graph.cg.yosys_to_compiled.at(graph.parsed.net_id_by_name("Q0")));
  ScanProtocolFaultRequest request;
  request.pattern.clock_ports = {"CLK"};
  request.pattern.scan_enable_port = "scan_en";
  request.pattern.scan_input_ports = {"scan_in"};
  request.pattern.scan_output_ports = {"scan_out_0"};
  request.pattern.functional_output_ports = {"Q0"};
  request.pattern.max_chain_length = 3;
  request.pattern.load_seqs[0] = {false, false, true};  // ff2, ff1, ff0: Q0 = 1
  request.pattern.loc_two_capture = true;
  request.faults = {{q0, 1}};  // slow to fall
  for (const bool d1 : {true, false}) {
    INFO("D1 = " << d1);
    request.pattern.capture_pi_values = {{"D0", false}, {"D1", d1}, {"D2", false}};
    const XDetection want = d1 ? XDetection::NONE : XDetection::DEFINITE;
    const auto graded = simulate_scan_protocol_faults_x(
        json, test::cell_map_path(), request, "fail+x", 1, kBlackbox, kUnknown);
    CHECK(graded.lanes.at(0).outcome == want);
    CHECK(simulate_scan_protocol_faults_x_reference(json, test::cell_map_path(), request,
                                                    "fail+x", kBlackbox, kUnknown)
              .at(0) == want);
  }
}

TEST_CASE("a tie marked unknown is an X source only in an X-mode graph",
          "[scan][xsim][ir]") {
  const std::string json = test::fixture_path("tiny_unknown_tie.json");
  const CachedGraph& graph = load_cached_graph(json, test::cell_map_path(), "fail+x");
  int unknown = 0;
  for (const SimNode& node : graph.cg.nodes) {
    unknown += node.type == GateType::CONSTX ? 1 : 0;
  }
  REQUIRE(unknown == 1);
  REQUIRE_THROWS_AS(load_cached_graph(json, test::cell_map_path(), "fail"), ParseError);
}

TEST_CASE("seeded dual-rail SAT: a definite test only through a load the seed makes",
          "[scan][xsim][seeded]") {
  // tiny_scan_view_seeded_x: Y = (ff0 & ff1) | (u_x & ff2), u_x unknown. ff0
  // stuck-at-0 needs ff0 = ff1 = 1 and is definite only with ff2 = 0.
  const std::string json = test::fixture_path("tiny_scan_view_seeded_x.json");
  const CachedGraph& g = load_cached_graph(json, test::cell_map_path(), "fail+x");
  const auto pis = atpg::ordered_pis(g.parsed, g.cg);
  const auto index = [&](const std::string& net) {
    return static_cast<uint32_t>(g.cg.yosys_to_compiled.at(g.parsed.net_id_by_name(net)));
  };
  CompactFault fault;
  fault.net_index = index("__ppi_ff0");
  fault.type = FaultType::SA0;
  fault.bit = 1;
  fault.sa_mask = 1ULL << 1;
  fault.exclusion = FaultExclusion::NONE;

  using Rows = std::vector<std::pair<std::string, std::vector<int>>>;
  // Both encodings: the fault's cone only, and the whole circuit (where every
  // input and unknown is in the out-cone, and must still be one value in both
  // machines).
  for (bool cone_restrict : {true, false}) {
    INFO("cone_restrict " << cone_restrict);
    std::vector<bool> seed;
    const auto solve = [&](const Rows& rows, std::map<std::string, bool>& out) {
      atpg::SatSolveOptions options;
      options.conflict_limit = -1;
      options.sat_timeout_seconds = 0;
      options.cone_restrict = cone_restrict;
      if (!rows.empty()) {
        options.seed_width = 2;
        for (const auto& [name, bits] : rows) {
          options.seeded_inputs.push_back(atpg::SeededInput{index(name), bits});
        }
        options.seed_out = &seed;
      }
      return atpg::solve_stuck_at_fault_x(g.cg, pis, fault, options, out);
    };

    std::map<std::string, bool> out;
    // ff0 = s0, ff1 = s0 ^ s1, ff2 = s1: the seed (1, 0) loads ff0 = ff1 = 1, ff2 = 0.
    REQUIRE(solve({{"__ppi_ff0", {0}}, {"__ppi_ff1", {0, 1}}, {"__ppi_ff2", {1}}},
                  out) == atpg::SatSolveResult::SAT);
    REQUIRE(seed == std::vector<bool>{true, false});
    REQUIRE(out.at("__ppi_ff0"));
    REQUIRE(out.at("__ppi_ff1"));
    REQUIRE_FALSE(out.at("__ppi_ff2"));
    // ff2 = s0: every load with ff0 = ff1 = 1 has ff2 = 1, which lets u_x through.
    REQUIRE(solve({{"__ppi_ff0", {0}}, {"__ppi_ff1", {0, 1}}, {"__ppi_ff2", {0}}},
                  out) == atpg::SatSolveResult::POSSIBLE);
    // No decompressor: definite.
    REQUIRE(solve({}, out) == atpg::SatSolveResult::SAT);
    REQUIRE_FALSE(out.at("__ppi_ff2"));
  }
}

TEST_CASE("X scan protocol: a transition fault acts from the launch edge",
          "[scan][xsim][xtransition]") {
  // tiny_scan_loop: chain ff_q3 -> ff_q1 -> ff_q2; a loop, set by C & q1, held
  // through hold = nand(q2, C) (reset by q2 & C); q3 captures A ^ loop, Z =
  // loop & B. The launch leaves q1 = 0, q2 = 1 after a set (q1 = 1, q2 = 0), so
  // hold falls and the loop resets. Slow to fall, hold still 1 after the launch
  // edge: the loop holds 1 at the capture, Z and q3 show it. With the fault
  // only from the capture sample on, the launched flops had reset the loop
  // fault-free first and the fault hid.
  const std::string json = test::fixture_path("tiny_scan_loop.json");
  const CachedGraph& graph = load_cached_graph(json, test::cell_map_path(), "fail+x", {});
  const auto hold = static_cast<uint32_t>(
      graph.cg.yosys_to_compiled.at(graph.parsed.net_id_by_name("hold")));
  for (const bool los : {false, true}) {
    INFO((los ? "launch on shift" : "launch on capture"));
    ScanProtocolFaultRequest request;
    request.pattern.clock_ports = {"CLK"};
    request.pattern.scan_enable_port = "scan_en";
    request.pattern.scan_input_ports = {"scan_in"};
    request.pattern.scan_output_ports = {"scan_out_0"};
    request.pattern.functional_output_ports = {"Y", "Z"};
    request.pattern.max_chain_length = 3;
    // ff_q2, ff_q1, ff_q3. LOC: q1 = 1, q2 = 0, q3 = 1; A = 0, B = 1 capture
    // q1 = 0, q2 = 1. LOS: q3 = 0, q1 = 1, q2 = 0 shift to q1 = 0, q2 = 1.
    request.pattern.load_seqs[0] = {false, true, !los};
    request.pattern.capture_pi_values = {{"A", false}, {"B", true}, {"C", true}};
    request.pattern.loc_two_capture = !los;
    request.pattern.los_two_capture = los;
    request.pattern.los_launch_scan_in[0] = false;
    request.faults = {{hold, 1}};
    CHECK(simulate_scan_protocol_faults_x(json, test::cell_map_path(), request, "fail+x",
                                          1, {}, {})
              .lanes.at(0)
              .outcome == XDetection::DEFINITE);
    CHECK(simulate_scan_protocol_faults_x_reference(json, test::cell_map_path(), request,
                                                    "fail+x", {}, {})
              .at(0) == XDetection::DEFINITE);
  }
}

TEST_CASE("X scan protocol grade == scalar X oracle on a loop, every fault",
          "[scan][xsim][reference]") {
  const std::string json = test::fixture_path("tiny_scan_loop.json");
  const CachedGraph& graph = load_cached_graph(json, test::cell_map_path(), "fail+x", {});
  const auto faults = every_fault(graph.cg);
  Bits bits{11u};
  size_t launched = 0;
  for (int p = 0; p < 24; ++p) {
    ScanProtocolFaultRequest request;
    request.pattern.clock_ports = {"CLK"};
    request.pattern.scan_enable_port = "scan_en";
    request.pattern.scan_input_ports = {"scan_in"};
    request.pattern.scan_output_ports = {"scan_out_0"};
    request.pattern.functional_output_ports = {"Y", "Z"};
    request.pattern.max_chain_length = 3;
    request.pattern.load_seqs[0] = {bits.next(), bits.next(), bits.next()};
    for (const char* pi : {"A", "B", "C"}) {
      request.pattern.capture_pi_values[pi] = bits.next();
    }
    set_launch(request.pattern, p, bits);
    request.faults = faults;
    const auto graded = simulate_scan_protocol_faults_x(json, test::cell_map_path(),
                                                        request, "fail+x", 2, {}, {});
    const auto reference = simulate_scan_protocol_faults_x_reference(
        json, test::cell_map_path(), request, "fail+x", {}, {});
    REQUIRE(reference.size() == faults.size());
    for (size_t i = 0; i < faults.size(); ++i) {
      INFO("pattern " << p << " net " << faults[i].compiled_net_index << " sa"
                      << int(faults[i].fault_type));
      REQUIRE(graded.lanes.at(i).outcome == reference[i]);
      launched += p % 3 != 0 && reference[i] != XDetection::NONE;
    }
  }
  REQUIRE(launched > 0);
}
