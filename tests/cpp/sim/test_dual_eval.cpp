#include <catch2/catch_test_macros.hpp>

#include <random>
#include <string>
#include <vector>

#include "ir/normalized_graph/cell_map.hpp"
#include "sim/gate_eval.hpp"
#include "sim/xsim/dual.hpp"
#include "sim/xsim/logic.hpp"

using namespace faultflow;

namespace {

std::vector<GateType> gate_types() {
  std::vector<GateType> out;
  for (const std::string& name : gate_type_names()) {
    out.push_back(gate_type_from_name(name));
  }
  return out;
}

Dual broadcast(Logic v) {
  switch (v) {
    case Logic::ZERO:
      return dual_known(0ULL);
    case Logic::ONE:
      return dual_known(~0ULL);
    default:
      return dual_x();
  }
}

// One lane of a dual word; Z marks the illegal (0, 0) code.
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

}  // namespace

TEST_CASE("Dual gate evaluation equals the X oracle, lane by lane", "[xsim][dual]") {
  std::mt19937 rng(7);
  const Logic values[3] = {Logic::ZERO, Logic::ONE, Logic::X};
  for (GateType type : gate_types()) {
    INFO(static_cast<int>(type));
    // Each lane an independent random input combination over all six inputs.
    for (int round = 0; round < 40; ++round) {
      std::array<Dual, 6> ins{};
      std::array<std::array<Logic, 6>, 64> lanes{};
      for (int i = 0; i < 6; ++i) {
        ins[i] = {0ULL, 0ULL};
        for (int b = 0; b < 64; ++b) {
          const Logic v = values[rng() % 3];
          lanes[b][i] = v;
          if (v != Logic::ZERO) {
            ins[i].h |= 1ULL << b;
          }
          if (v != Logic::ONE) {
            ins[i].l |= 1ULL << b;
          }
        }
      }
      const Dual out = eval_gate_dual(type, ins);
      for (int b = 0; b < 64; ++b) {
        const std::vector<Logic> in(lanes[b].begin(), lanes[b].end());
        REQUIRE(lane(out, b) == eval_gate_x(type, in));
      }
    }
  }
}

TEST_CASE("Dual gate evaluation with no X is the two-valued word and its complement",
          "[xsim][dual]") {
  std::mt19937_64 rng(11);
  for (GateType type : gate_types()) {
    for (int round = 0; round < 20; ++round) {
      std::vector<uint64_t> words(6);
      std::array<Dual, 6> ins{};
      for (int i = 0; i < 6; ++i) {
        words[i] = rng();
        ins[i] = dual_known(words[i]);
      }
      const Dual out = eval_gate_dual(type, ins);
      REQUIRE(out.h == eval_gate(type, words));
      REQUIRE(out.l == ~out.h);
    }
  }
}

TEST_CASE("Dual helpers: an exact mux and the clock gate", "[xsim][dual]") {
  const Logic values[3] = {Logic::ZERO, Logic::ONE, Logic::X};
  for (Logic s : values) {
    for (Logic a : values) {
      for (Logic b : values) {
        REQUIRE(lane(dual_mux(broadcast(s), broadcast(a), broadcast(b)), 0) ==
                logic_mux(s, a, b));
        REQUIRE(lane(dual_and(broadcast(a), broadcast(b)), 0) == logic_and(a, b));
        REQUIRE(lane(dual_or(broadcast(a), broadcast(b)), 0) == logic_or(a, b));
      }
      REQUIRE(lane(dual_not(broadcast(a)), 0) == logic_not(a));
    }
  }
  for (GateType type : {GateType::ICG_POS, GateType::ICG_NEG}) {
    for (Logic clk : values) {
      for (Logic en : values) {
        for (Logic held : values) {
          Logic xlatch = held;
          const Logic want = eval_icg_x(type, clk, en, xlatch);
          Dual latch = broadcast(held);
          const Dual got = eval_icg_dual(type, broadcast(clk), broadcast(en), latch);
          REQUIRE(lane(got, 0) == want);
          REQUIRE(lane(latch, 0) == xlatch);
        }
      }
    }
  }
}
