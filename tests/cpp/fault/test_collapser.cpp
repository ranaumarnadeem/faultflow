#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <vector>

#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"

using namespace faultflow;

// ---------------------------------------------------------------------------
// Shared test helpers
// ---------------------------------------------------------------------------

namespace {

// Returns the INDEX in faults[] for the fault at (compiled_net, type), or
// UINT32_MAX when not found.
uint32_t fault_idx(const std::vector<CompactFault>& faults, uint32_t cidx,
                   FaultType t) {
  for (uint32_t i = 0; i < static_cast<uint32_t>(faults.size()); ++i) {
    if (faults[i].net_index == cidx && faults[i].type == t) return i;
  }
  return UINT32_MAX;
}

// True when the fault for (cidx, t) has been collapsed into something.
bool is_collapsed(const std::vector<CompactFault>& faults, uint32_t cidx,
                  FaultType t) {
  const uint32_t idx = fault_idx(faults, cidx, t);
  return idx != UINT32_MAX && faults[idx].collapsed_into != UINT32_MAX;
}

// Compiled index for a Yosys net ID.
uint32_t ci(const CompiledSimGraph& cg, int yosys_id) {
  return static_cast<uint32_t>(cg.yosys_to_compiled.at(yosys_id));
}

}  // namespace

// ---------------------------------------------------------------------------
// Original regression test (kept)
// ---------------------------------------------------------------------------

TEST_CASE("Primitive collapser marks INV input equivalents", "[collapser]") {
  const NormalizedGraph ng = test::load_normalized("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const auto collapsed = std::count_if(
      faults.begin(), faults.end(),
      [](const CompactFault& f) { return f.collapsed_into != UINT32_MAX; });

  REQUIRE(collapsed >= 2);
}

// ---------------------------------------------------------------------------
// Per-gate exact-collapse tests
// All tiny_*.json fixtures follow: inputs from net 2, output(s) after.
// 1-input (INV/BUF): A=2, Y=3
// 2-input (AND2/OR2/NAND2/NOR2): A=2, B=3, Y=4
// ---------------------------------------------------------------------------

TEST_CASE("INV: both polarities collapse, outputs are representatives",
          "[collapser]") {
  // in/SA0 == out/SA1 ; in/SA1 == out/SA0
  const NormalizedGraph ng = test::load_normalized("tiny_inv.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_inv.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_Y = ci(cg, 3);

  const uint32_t iY_sa0 = fault_idx(faults, ci_Y, FaultType::SA0);
  const uint32_t iY_sa1 = fault_idx(faults, ci_Y, FaultType::SA1);
  REQUIRE(iY_sa0 != UINT32_MAX);
  REQUIRE(iY_sa1 != UINT32_MAX);

  // A/SA0 -> Y/SA1 (inversion flips polarity)
  const uint32_t iA_sa0 = fault_idx(faults, ci_A, FaultType::SA0);
  REQUIRE(iA_sa0 != UINT32_MAX);
  REQUIRE(faults[iA_sa0].collapsed_into == iY_sa1);

  // A/SA1 -> Y/SA0
  const uint32_t iA_sa1 = fault_idx(faults, ci_A, FaultType::SA1);
  REQUIRE(iA_sa1 != UINT32_MAX);
  REQUIRE(faults[iA_sa1].collapsed_into == iY_sa0);

  // Output representatives must NOT be collapsed
  REQUIRE(faults[iY_sa0].collapsed_into == UINT32_MAX);
  REQUIRE(faults[iY_sa1].collapsed_into == UINT32_MAX);
}

TEST_CASE("BUF: both polarities collapse, outputs are representatives",
          "[collapser]") {
  // in/SA0 == out/SA0 ; in/SA1 == out/SA1
  const NormalizedGraph ng = test::load_normalized("tiny_buf.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_buf.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_Y = ci(cg, 3);

  const uint32_t iY_sa0 = fault_idx(faults, ci_Y, FaultType::SA0);
  const uint32_t iY_sa1 = fault_idx(faults, ci_Y, FaultType::SA1);
  REQUIRE(iY_sa0 != UINT32_MAX);
  REQUIRE(iY_sa1 != UINT32_MAX);

  // A/SA0 -> Y/SA0 (identity)
  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA0)].collapsed_into == iY_sa0);
  // A/SA1 -> Y/SA1
  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA1)].collapsed_into == iY_sa1);

  // Output representatives must NOT be collapsed
  REQUIRE(faults[iY_sa0].collapsed_into == UINT32_MAX);
  REQUIRE(faults[iY_sa1].collapsed_into == UINT32_MAX);
}

TEST_CASE("AND2: only SA0 inputs collapse, SA1 inputs do not", "[collapser]") {
  // Controlling value = 0; in/SA0 -> out/SA0 ; in/SA1 NOT collapsed (dominance only)
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_B = ci(cg, 3);
  const uint32_t ci_Y = ci(cg, 4);

  const uint32_t iY_sa0 = fault_idx(faults, ci_Y, FaultType::SA0);
  REQUIRE(iY_sa0 != UINT32_MAX);

  // Both SA0 inputs collapse into Y/SA0
  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA0)].collapsed_into == iY_sa0);
  REQUIRE(faults[fault_idx(faults, ci_B, FaultType::SA0)].collapsed_into == iY_sa0);

  // SA1 inputs must NOT be collapsed (dominance, not equivalence)
  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA1) == false);
  REQUIRE(is_collapsed(faults, ci_B, FaultType::SA1) == false);

  // Output representative is NOT collapsed; Y/SA1 also stays free
  REQUIRE(faults[iY_sa0].collapsed_into == UINT32_MAX);
  REQUIRE(is_collapsed(faults, ci_Y, FaultType::SA1) == false);
}

TEST_CASE("OR2: only SA1 inputs collapse, SA0 inputs do not", "[collapser]") {
  // Controlling value = 1; in/SA1 -> out/SA1 ; in/SA0 NOT collapsed
  const NormalizedGraph ng = test::load_normalized("tiny_or2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_or2.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_B = ci(cg, 3);
  const uint32_t ci_Y = ci(cg, 4);

  const uint32_t iY_sa1 = fault_idx(faults, ci_Y, FaultType::SA1);
  REQUIRE(iY_sa1 != UINT32_MAX);

  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA1)].collapsed_into == iY_sa1);
  REQUIRE(faults[fault_idx(faults, ci_B, FaultType::SA1)].collapsed_into == iY_sa1);

  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA0) == false);
  REQUIRE(is_collapsed(faults, ci_B, FaultType::SA0) == false);

  REQUIRE(faults[iY_sa1].collapsed_into == UINT32_MAX);
  REQUIRE(is_collapsed(faults, ci_Y, FaultType::SA0) == false);
}

TEST_CASE("NAND2: SA0 inputs collapse into out/SA1, SA1 inputs do not",
          "[collapser]") {
  // Controlling value = 0; in/SA0 -> out/SA1 (inverted output)
  const NormalizedGraph ng = test::load_normalized("tiny_nand2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_nand2.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_B = ci(cg, 3);
  const uint32_t ci_Y = ci(cg, 4);

  const uint32_t iY_sa1 = fault_idx(faults, ci_Y, FaultType::SA1);
  REQUIRE(iY_sa1 != UINT32_MAX);

  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA0)].collapsed_into == iY_sa1);
  REQUIRE(faults[fault_idx(faults, ci_B, FaultType::SA0)].collapsed_into == iY_sa1);

  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA1) == false);
  REQUIRE(is_collapsed(faults, ci_B, FaultType::SA1) == false);

  REQUIRE(faults[iY_sa1].collapsed_into == UINT32_MAX);
  REQUIRE(is_collapsed(faults, ci_Y, FaultType::SA0) == false);
}

TEST_CASE("NOR2: SA1 inputs collapse into out/SA0, SA0 inputs do not",
          "[collapser]") {
  // Controlling value = 1; in/SA1 -> out/SA0 (inverted output)
  const NormalizedGraph ng = test::load_normalized("tiny_nor2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_nor2.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);
  const uint32_t ci_B = ci(cg, 3);
  const uint32_t ci_Y = ci(cg, 4);

  const uint32_t iY_sa0 = fault_idx(faults, ci_Y, FaultType::SA0);
  REQUIRE(iY_sa0 != UINT32_MAX);

  REQUIRE(faults[fault_idx(faults, ci_A, FaultType::SA1)].collapsed_into == iY_sa0);
  REQUIRE(faults[fault_idx(faults, ci_B, FaultType::SA1)].collapsed_into == iY_sa0);

  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA0) == false);
  REQUIRE(is_collapsed(faults, ci_B, FaultType::SA0) == false);

  REQUIRE(faults[iY_sa0].collapsed_into == UINT32_MAX);
  REQUIRE(is_collapsed(faults, ci_Y, FaultType::SA1) == false);
}

// ---------------------------------------------------------------------------
// Multi-fanout guard test
// ---------------------------------------------------------------------------

TEST_CASE("Multi-fanout stem not collapsed, fanout-free branches are",
          "[collapser]") {
  // tiny_reconverge.json: A(net2) fans out to both u0(AND2) and u1(AND2).
  // After fanout splitting the stem has fanout=2 → its faults must NOT be
  // collapsed. The branch nets (fanout=1, each feeds exactly one AND2) ARE
  // collapsed by the AND2 SA0 rule.
  const NormalizedGraph ng = test::load_normalized("tiny_reconverge.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_reconverge.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  const uint32_t ci_A = ci(cg, 2);  // PI A stem

  // Stem A has fanout > 1 — must NOT be collapsed
  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA0) == false);
  REQUIRE(is_collapsed(faults, ci_A, FaultType::SA1) == false);

  // Other faults (B/C SA0 inputs, n5/n6 SA1 inputs) ARE collapsed — the guard
  // is not blocking all collapses.
  const auto total_collapsed = std::count_if(
      faults.begin(), faults.end(),
      [](const CompactFault& f) { return f.collapsed_into != UINT32_MAX; });
  REQUIRE(total_collapsed > 0);

  // For each AND2 node: its in0 (the A-derived branch, fanout-free) must have
  // its SA0 fault collapsed. This directly tests that the branch net — not the
  // multi-fanout stem — was targeted by the collapse.
  for (const SimNode& sn : cg.nodes) {
    if (sn.type != GateType::AND2) continue;
    if (sn.in0 == UNUSED_INPUT) continue;
    // Only A-derived branch inputs (same Yosys ID 2, different compiled index)
    if (static_cast<uint32_t>(cg.compiled_to_yosys[sn.in0]) != 2u) continue;
    if (sn.in0 == ci_A) continue;  // skip if somehow the stem itself (shouldn't happen)
    REQUIRE(is_collapsed(faults, sn.in0, FaultType::SA0));
  }
}

// ---------------------------------------------------------------------------
// Equivalence cross-check via GoldenRefSim
// ---------------------------------------------------------------------------

TEST_CASE("Collapsed pairs are detection-equivalent on exhaustive input space",
          "[collapser]") {
  // For every collapse a -> b, verify is_detected(a) == is_detected(b) under
  // every possible input combination. Uses tiny_and2.json (2 PIs, 4 vectors).
  const NormalizedGraph ng = test::load_normalized("tiny_and2.json");
  const CompiledSimGraph cg = test::load_compiled("tiny_and2.json");
  const auto faults = collapse_primitive_faults(ng, cg, enumerate_faults(ng, cg));

  // Build the complete 2^n input space
  std::vector<int> pi_yosys_ids;
  for (int cidx : cg.pi_nets) {
    pi_yosys_ids.push_back(cg.compiled_to_yosys[static_cast<size_t>(cidx)]);
  }
  const auto vs = test::generate_complete_input_space(pi_yosys_ids);

  GoldenRefSim golden;
  for (const auto& fault : faults) {
    if (fault.collapsed_into == UINT32_MAX) continue;
    const CompactFault& rep = faults[fault.collapsed_into];

    for (const auto& vec : vs.vectors) {
      const auto ff = golden.simulate_fault_free(cg, vec);
      const auto fa = golden.simulate_with_fault(cg, vec, fault);
      const auto fr = golden.simulate_with_fault(cg, vec, rep);
      REQUIRE(golden.is_detected(cg, ff, fa) == golden.is_detected(cg, ff, fr));
    }
  }
}
