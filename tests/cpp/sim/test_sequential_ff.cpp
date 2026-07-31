#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

namespace {

TestCycle cycle(std::initializer_list<std::pair<const int, bool>> inputs,
                bool sample = true) {
  TestCycle c;
  c.inputs = std::map<int, bool>(inputs);
  c.sample_outputs = sample;
  return c;
}

bool sampled_q(const std::vector<std::map<int, bool>>& samples, int q_net) {
  REQUIRE(!samples.empty());
  return samples.back().at(q_net);
}

}  // namespace

TEST_CASE("FF metadata normalizes and tags clock nets", "[sequential]") {
  const NormalizedGraph ng = test::load_normalized("tiny_dff.json");
  REQUIRE(ng.nodes.size() >= 1);
  const auto node_it = std::find_if(
      ng.nodes.begin(), ng.nodes.end(),
      [](const auto& item) { return item.second.type == NodeType::FF; });
  REQUIRE(node_it != ng.nodes.end());
  const NormNode& node = node_it->second;
  REQUIRE(node.ff_config.trigger == TriggerType::POSEDGE);
  REQUIRE(node.ff_config.clock_net == 2);
  REQUIRE(node.ff_config.data_net == 3);
  REQUIRE(node.output_pins.at("Q") == 4);
  REQUIRE(ng.clocks.count(2) == 1);
  REQUIRE(ng.nets.at(2).is_clock);
}

TEST_CASE("Compiled graph stores FF configs outside mutable state",
          "[sequential]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  REQUIRE(cg.ff_configs.size() == 1);
  REQUIRE(cg.ff_nodes.size() == 1);
  const SimNode& ff = cg.nodes.at(cg.ff_nodes.at(0));
  REQUIRE(ff.type == GateType::DFF);
  REQUIRE(ff.ff_cfg == 0);
  REQUIRE(cg.ff_configs.at(0).trigger == TriggerType::POSEDGE);
  REQUIRE(ff.in0 != UNUSED_INPUT);
  REQUIRE(ff.in1 != UNUSED_INPUT);
}

TEST_CASE("GoldenRefSim captures posedge and negedge DFFs",
          "[sequential]") {
  GoldenRefSim sim;

  {
    const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
    TestVector vec;
    vec.cycles = {
        cycle({{2, false}, {3, true}}, false),
        cycle({{2, true}, {3, true}}, true),
    };
    const auto samples = sim.simulate_sequence_fault_free(cg, vec);
    REQUIRE(sampled_q(samples, 4));
  }

  {
    const CompiledSimGraph cg = test::load_compiled("tiny_dff_neg.json");
    TestVector vec;
    vec.cycles = {
        cycle({{2, true}, {3, true}}, false),
        cycle({{2, false}, {3, true}}, true),
    };
    const auto samples = sim.simulate_sequence_fault_free(cg, vec);
    REQUIRE(sampled_q(samples, 4));
  }
}

TEST_CASE("GoldenRefSim applies async active-low controls",
          "[sequential]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dffsr.json");
  GoldenRefSim sim;

  TestVector clear_vec;
  clear_vec.initial_ff_state[0] = true;
  clear_vec.cycles = {cycle({{2, false}, {3, true}, {4, false}, {5, true}})};
  REQUIRE_FALSE(sampled_q(sim.simulate_sequence_fault_free(cg, clear_vec), 6));

  TestVector preset_vec;
  preset_vec.cycles = {cycle({{2, false}, {3, false}, {4, true}, {5, false}})};
  REQUIRE(sampled_q(sim.simulate_sequence_fault_free(cg, preset_vec), 6));

  TestVector conflict_vec;
  conflict_vec.initial_ff_state[0] = true;
  conflict_vec.cycles = {cycle({{2, false}, {3, true}, {4, false}, {5, false}})};
  REQUIRE_FALSE(
      sampled_q(sim.simulate_sequence_fault_free(cg, conflict_vec), 6));
}

TEST_CASE("Bit-parallel sequential sim matches GoldenRefSim for D-pin fault",
          "[sequential]") {
  const NormalizedGraph ng = test::load_normalized("tiny_dff.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  const uint32_t d_net = cg.yosys_to_compiled.at(3);

  CompactFault d_sa0;
  d_sa0.net_index = d_net;
  d_sa0.type = FaultType::SA0;

  TestVector vec;
  vec.cycles = {
      cycle({{2, false}, {3, true}}, false),
      cycle({{2, true}, {3, true}}, true),
  };

  GoldenRefSim golden;
  const auto ff = golden.simulate_sequence_fault_free(cg, vec);
  const auto faulty = golden.simulate_sequence_with_fault(cg, vec, d_sa0);
  REQUIRE(golden.is_sequence_detected(cg, ff, faulty));

  BitParallelSim parallel;
  REQUIRE(parallel.simulate_single_fault(cg, vec, d_sa0));
  (void)ng;
}

TEST_CASE("D-pin fault without capture remains undetected not redundant",
          "[sequential]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  CompactFault d_sa0;
  d_sa0.net_index = cg.yosys_to_compiled.at(3);
  d_sa0.type = FaultType::SA0;
  d_sa0.status = FaultStatus::UNDETECTED;

  TestVector vec;
  vec.cycles = {cycle({{2, false}, {3, true}}, true)};

  BitParallelSim parallel;
  REQUIRE_FALSE(parallel.simulate_single_fault(cg, vec, d_sa0));
  REQUIRE(d_sa0.status == FaultStatus::UNDETECTED);
}

TEST_CASE("Clock net faults remain excluded by default for FF metadata",
          "[sequential][fault]") {
  const NormalizedGraph ng = test::load_normalized("tiny_dff.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_dff.json");
  const auto faults = enumerate_faults(ng, cg);
  const uint32_t clk = cg.yosys_to_compiled.at(2);
  int excluded = 0;
  for (const auto& fault : faults) {
    if (fault.net_index == clk && fault.exclusion == FaultExclusion::CLOCK) {
      ++excluded;
    }
  }
  REQUIRE(excluded == 2);
}

TEST_CASE("Scannable async-reset FF gates reset during shift, applies at capture",
          "[sequential][scan]") {
  // sdfrtp = scan FF with async reset. nets: CLK=2 D=3 SCD=4 SCE=5 RESET_B=6 Q=7.
  const CompiledSimGraph cg = test::load_compiled("tiny_sdfrtp.json");
  GoldenRefSim golden;
  BitParallelSim parallel;

  // (1) Shift: SCE=1, RESET_B=0 (asserted). The reset must be GATED OFF during
  //     shift, so the FF captures the scan-in SCD=1 instead of resetting.
  {
    TestVector vec;
    vec.cycles = {
        cycle({{2, false}, {3, false}, {4, true}, {5, true}, {6, false}}, false),
        cycle({{2, true}, {3, false}, {4, true}, {5, true}, {6, false}}, true),
    };
    REQUIRE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 7));
  }

  // (2) Capture: SCE=0, RESET_B=0 (asserted) → async reset forces Q=0 even though
  //     the FF started at 1.
  {
    TestVector vec;
    vec.initial_ff_state = {{0u, true}};  // single FF (compiled index 0) starts at Q=1
    vec.cycles = {
        cycle({{2, false}, {3, true}, {4, false}, {5, false}, {6, false}}, true),
    };
    REQUIRE_FALSE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 7));
  }

  // (3) Capture: SCE=0, RESET_B=1 (inactive), D=1 → functional capture Q=1.
  {
    TestVector vec;
    vec.cycles = {
        cycle({{2, false}, {3, true}, {4, false}, {5, false}, {6, true}}, false),
        cycle({{2, true}, {3, true}, {4, false}, {5, false}, {6, true}}, true),
    };
    REQUIRE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 7));
  }

  // (4) Golden == bit-parallel: a scan-in SA0 corrupts the shifted bit (Q=0 not
  //     1); both engines must agree it is detected.
  {
    TestVector vec;
    vec.cycles = {
        cycle({{2, false}, {3, false}, {4, true}, {5, true}, {6, false}}, false),
        cycle({{2, true}, {3, false}, {4, true}, {5, true}, {6, false}}, true),
    };
    CompactFault scd_sa0;
    scd_sa0.net_index = cg.yosys_to_compiled.at(4);
    scd_sa0.type = FaultType::SA0;
    const auto ff = golden.simulate_sequence_fault_free(cg, vec);
    const auto faulty = golden.simulate_sequence_with_fault(cg, vec, scd_sa0);
    REQUIRE(golden.is_sequence_detected(cg, ff, faulty));
    REQUIRE(parallel.simulate_single_fault(cg, vec, scd_sa0));
  }
}

// Regression for the edfxtp data-enable (DE) bug: the cell map declared
// `inputs: ["CLK", "D"]` with no `enable` in the ff metadata, so DE was never
// bound anywhere and an enable flip-flop simulated as an unconditional D-FF
// (Q <= D on every edge, DE completely dropped). Sky130's own Liberty next_state
// for edfxtp is "(D&DE) | (IQ&!DE)" -- a synchronous D/hold mux gated by DE,
// active-high. These tests pin that exact behavior end to end.

TEST_CASE("edfxtp FF metadata resolves the DE enable control",
          "[sequential][enable]") {
  const NormalizedGraph ng = test::load_normalized("tiny_edfxtp.json");
  const auto node_it = std::find_if(
      ng.nodes.begin(), ng.nodes.end(),
      [](const auto& item) { return item.second.type == NodeType::FF; });
  REQUIRE(node_it != ng.nodes.end());
  const NormNode& node = node_it->second;
  REQUIRE(node.ff_config.enable.present);
  REQUIRE(node.ff_config.enable.net == 4);  // DE net id
  REQUIRE(node.ff_config.enable.polarity == Polarity::ACTIVE_HIGH);
  REQUIRE_FALSE(node.ff_config.clear.present);
}

TEST_CASE("Compiled graph wires DE into the FF's enable slot",
          "[sequential][enable]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  REQUIRE(cg.ff_configs.size() == 1);
  const SimNode& ff = cg.nodes.at(cg.ff_nodes.at(0));
  REQUIRE(cg.ff_configs.at(0).has_enable);
  REQUIRE(cg.ff_configs.at(0).enable_polarity == Polarity::ACTIVE_HIGH);
  REQUIRE(ff.in2 != UNUSED_INPUT);
  REQUIRE(ff.in2 == cg.yosys_to_compiled.at(4));  // DE
}

TEST_CASE("edfxtp captures D while DE is active", "[sequential][enable]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  GoldenRefSim golden;

  TestVector vec;
  vec.cycles = {
      cycle({{2, false}, {3, true}, {4, true}}, false),  // CLK=0 D=1 DE=1
      cycle({{2, true}, {3, true}, {4, true}}, true),    // edge: capture D=1
  };
  REQUIRE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 5));
}

TEST_CASE("edfxtp holds Q (does not fall to D=1) while DE is inactive",
          "[sequential][enable]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  GoldenRefSim golden;

  // Q starts at 0 (default). DE=0 the whole time, D=1: a buggy "DE dropped,
  // always capture D" implementation would set Q=1 here; the real gated
  // behavior must hold Q at its initial 0.
  TestVector vec;
  vec.cycles = {
      cycle({{2, false}, {3, true}, {4, false}}, false),  // CLK=0 D=1 DE=0
      cycle({{2, true}, {3, true}, {4, false}}, true),    // edge, DE=0: hold
  };
  REQUIRE_FALSE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 5));
}

TEST_CASE("edfxtp holds Q=1 (does not fall to D=0) while DE is inactive",
          "[sequential][enable]") {
  // The complementary hold direction -- proves this is a genuine D/Q mux, not
  // just "DE=0 forces 0" (which the previous test alone could not rule out).
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  GoldenRefSim golden;

  TestVector vec;
  vec.initial_ff_state[0] = true;  // Q starts at 1
  vec.cycles = {
      cycle({{2, false}, {3, false}, {4, false}}, false),  // CLK=0 D=0 DE=0
      cycle({{2, true}, {3, false}, {4, false}}, true),    // edge, DE=0: hold
  };
  REQUIRE(sampled_q(golden.simulate_sequence_fault_free(cg, vec), 5));
}

TEST_CASE(
    "Bit-parallel matches GoldenRefSim: a D fault is observable only when "
    "DE is active",
    "[sequential][enable]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_edfxtp.json");
  GoldenRefSim golden;
  BitParallelSim parallel;

  CompactFault d_sa0;
  d_sa0.net_index = cg.yosys_to_compiled.at(3);
  d_sa0.type = FaultType::SA0;

  // DE=1: D genuinely flows through to Q, so forcing D=0 changes the capture.
  TestVector enabled_vec;
  enabled_vec.cycles = {
      cycle({{2, false}, {3, true}, {4, true}}, false),
      cycle({{2, true}, {3, true}, {4, true}}, true),
  };
  const auto ff = golden.simulate_sequence_fault_free(cg, enabled_vec);
  const auto faulty =
      golden.simulate_sequence_with_fault(cg, enabled_vec, d_sa0);
  REQUIRE(golden.is_sequence_detected(cg, ff, faulty));
  REQUIRE(parallel.simulate_single_fault(cg, enabled_vec, d_sa0));

  // DE=0: D is ignored (Q holds), so the same D stuck-at fault has no effect.
  CompactFault d_sa0_disabled;
  d_sa0_disabled.net_index = cg.yosys_to_compiled.at(3);
  d_sa0_disabled.type = FaultType::SA0;
  d_sa0_disabled.status = FaultStatus::UNDETECTED;
  TestVector disabled_vec;
  disabled_vec.cycles = {
      cycle({{2, false}, {3, true}, {4, false}}, false),
      cycle({{2, true}, {3, true}, {4, false}}, true),
  };
  REQUIRE_FALSE(parallel.simulate_single_fault(cg, disabled_vec, d_sa0_disabled));
}

TEST_CASE("Sync reset fixture resets through D-cone logic", "[sequential]") {
  const CompiledSimGraph cg = test::load_compiled("tiny_sync_reset.json");
  GoldenRefSim sim;

  TestVector vec;
  vec.cycles = {
      cycle({{2, false}, {3, true}, {4, true}}, false),
      cycle({{2, true}, {3, true}, {4, true}}, true),
      cycle({{2, false}, {3, false}, {4, true}}, false),
      cycle({{2, true}, {3, false}, {4, true}}, true),
  };
  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  REQUIRE(samples.size() == 2);
  REQUIRE_FALSE(samples.at(0).at(7));
  REQUIRE(samples.at(1).at(7));
}
