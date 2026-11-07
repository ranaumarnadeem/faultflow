#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <map>
#include <string>
#include <vector>

#include "fault/effect/fault_batch.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;

TEST_CASE("GoldenRefSim tiny_inv detection", "[golden_ref][tiny_fixtures]") {
  const ParsedGraph pg = test::load_parsed("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const int po = pg.net_id_by_name("Y");
  GoldenRefSim sim;

  TestVector v0;
  v0.inputs[pg.net_id_by_name("A")] = false;
  const auto ff = sim.simulate_fault_free(cg, v0);
  REQUIRE(ff.at(po) == true);

  CompactFault sa0;
  sa0.net_index = cg.yosys_to_compiled.at(po);
  sa0.type = FaultType::SA0;
  const auto faulty0 = sim.simulate_with_fault(cg, v0, sa0);
  REQUIRE(sim.is_detected(cg, ff, faulty0));

  CompactFault sa1;
  sa1.net_index = cg.yosys_to_compiled.at(po);
  sa1.type = FaultType::SA1;
  const auto faulty1a = sim.simulate_with_fault(cg, v0, sa1);
  REQUIRE_FALSE(sim.is_detected(cg, ff, faulty1a));

  TestVector v1;
  v1.inputs[pg.net_id_by_name("A")] = true;
  const auto ff1 = sim.simulate_fault_free(cg, v1);
  const auto faulty1b = sim.simulate_with_fault(cg, v1, sa1);
  REQUIRE(sim.is_detected(cg, ff1, faulty1b));
}

TEST_CASE("Two pins of a gate on one net are a fault site each",
          "[golden_ref][tiny_fixtures]") {
  // Y = NAND(X, X). A stuck-at on one pin leaves the other reading X.
  const ParsedGraph pg = test::load_parsed("tiny_tied_gate_pins.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_tied_gate_pins.json");
  const int x = pg.net_id_by_name("X");
  std::map<std::string, uint32_t> branch_of_pin;
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    if (cg.compiled_to_yosys[cidx] == x &&
        cg.net_sites[cidx].kind == SiteKind::BRANCH) {
      CHECK(cg.net_sites[cidx].consumer_instance == "g0");
      branch_of_pin[cg.net_sites[cidx].input_pin] = cidx;
    }
  }
  REQUIRE(branch_of_pin.size() == 2);
  REQUIRE(branch_of_pin.count("A") == 1);
  REQUIRE(branch_of_pin.count("B") == 1);

  GoldenRefSim golden;
  const auto detected = [&](uint32_t net, FaultType type, bool value) {
    TestVector vec;
    vec.inputs[x] = value;
    CompactFault fault;
    fault.net_index = net;
    fault.type = type;
    return golden.is_detected(cg, golden.simulate_fault_free(cg, vec),
                              golden.simulate_with_fault(cg, vec, fault));
  };
  for (const auto& [pin, net] : branch_of_pin) {
    INFO(pin);
    // Stuck at 1: ~(1 & X) = ~X, the fault-free output on either value.
    CHECK_FALSE(detected(net, FaultType::SA1, false));
    CHECK_FALSE(detected(net, FaultType::SA1, true));
    // Stuck at 0: the output is 1, which X = 1 tells from the fault-free 0.
    CHECK(detected(net, FaultType::SA0, true));
    CHECK_FALSE(detected(net, FaultType::SA0, false));
  }
}

TEST_CASE("GoldenRefSim reads an observed stem's own value rather than its branch's",
          "[golden_ref][tiny_fixtures]") {
  // tiny_po_fanout: output Y = BUF(A) also feeds Z1 = Y & B and Z2 = Y & B,
  // so the compiler splits Y into two fanout branches. A branch carries its
  // stem's Yosys id, and GoldenRefSim's results are keyed by Yosys id.
  const ParsedGraph pg = test::load_parsed("tiny_po_fanout.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_po_fanout.json");
  const int y = pg.net_id_by_name("Y");
  std::vector<uint32_t> branches;
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    if (cg.compiled_to_yosys[cidx] == y &&
        cg.net_sites[cidx].kind == SiteKind::BRANCH) {
      branches.push_back(cidx);
    }
  }
  REQUIRE(branches.size() == 2);
  const uint32_t last = *std::max_element(branches.begin(), branches.end());
  REQUIRE(last > static_cast<uint32_t>(cg.yosys_to_compiled.at(y)));

  const auto inputs = [&](bool a, bool b) {
    TestVector vec;
    vec.inputs[pg.net_id_by_name("A")] = a;
    vec.inputs[pg.net_id_by_name("B")] = b;
    return vec;
  };
  // Y's last branch stuck at 1 while Y = 0. B = 0 holds both AND outputs at
  // 0, so the fault reaches no output, and Y itself is untouched.
  CompactFault fault;
  fault.net_index = last;
  fault.type = FaultType::SA1;
  fault.bit = 1;
  fault.sa_mask = 1ULL << 1;
  const TestVector vec = inputs(false, false);

  FaultBatch batch;
  batch.faults[0] = fault;
  batch.size = 1;
  batch.mask = fault.sa_mask;
  REQUIRE(BitParallelSim().simulate_batch(cg, vec, batch) == 0);

  GoldenRefSim golden;
  const auto ff = golden.simulate_fault_free(cg, vec);
  const auto faulty = golden.simulate_with_fault(cg, vec, fault);
  CHECK(faulty.at(y) == ff.at(y));
  CHECK_FALSE(golden.is_detected(cg, ff, faulty));

  // The same through the sequential snapshot (a one-cycle sequence) ...
  TestVector seq;
  TestCycle cycle;
  cycle.inputs = vec.inputs;
  seq.cycles = {cycle};
  CHECK_FALSE(golden.is_sequence_detected(
      cg, golden.simulate_sequence_fault_free(cg, seq),
      golden.simulate_sequence_with_fault(cg, seq, fault)));

  // ... and the two-frame transition oracle: the branch falls with Y
  // (A: 1 -> 0), but B = 0 still blocks its slow-to-fall effect.
  CHECK_FALSE(golden.simulate_transition_fault(cg, inputs(true, false), vec, fault));
}
