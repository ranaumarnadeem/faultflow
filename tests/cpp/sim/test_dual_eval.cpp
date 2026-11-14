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

TEST_CASE("The known-lane shortcut equals both sides of the cubes, few unknowns or none",
          "[xsim][dual]") {
  // Known words with an X sprinkled on some lanes of some inputs (or none: the
  // shortcut), and X only on inputs the function ignores (the shortcut too): the
  // result is both sides' and, lane by lane, the X oracle's.
  std::mt19937_64 rng(13);
  for (GateType type : gate_types()) {
    INFO(static_cast<int>(type));
    const DualGate* gate = dual_gate(type);
    REQUIRE(gate != nullptr);
    const int arity = gate->arity;
    REQUIRE(arity <= 6);
    for (int round = 0; round < 60; ++round) {
      std::array<Dual, 6> ins{};
      for (int i = 0; i < 6; ++i) {
        ins[i] = dual_known(rng());
        const int mode = static_cast<int>(rng() % 4);
        if (mode == 1) {
          const uint64_t lane = 1ULL << (rng() % 64);
          ins[i].h |= lane;
          ins[i].l |= lane;
        } else if (mode == 2 && i >= arity) {
          ins[i] = dual_x();
        }
      }
      const Dual got = eval_gate_dual(type, ins);
      const Dual cubes = eval_gate_dual_cubes(type, ins);
      REQUIRE(got.h == cubes.h);
      REQUIRE(got.l == cubes.l);
      for (int b = 0; b < 64; ++b) {
        std::vector<Logic> in;
        for (int i = 0; i < 6; ++i) {
          in.push_back(lane(ins[i], b));
        }
        REQUIRE(lane(got, b) == eval_gate_x(type, in));
      }
    }
  }
}

TEST_CASE("dual_gate: relevant is exactly the inputs the function depends on",
          "[xsim][dual]") {
  for (GateType type : gate_types()) {
    INFO(static_cast<int>(type));
    const DualGate* gate = dual_gate(type);
    REQUIRE(gate != nullptr);
    uint8_t depends = 0;
    for (int m = 0; m < 64; ++m) {
      std::vector<bool> bits(6);
      for (int i = 0; i < 6; ++i) {
        bits[i] = ((m >> i) & 1) != 0;
      }
      const bool base = eval_gate_scalar(type, bits);
      for (int i = 0; i < 6; ++i) {
        std::vector<bool> flipped = bits;
        flipped[i] = !flipped[i];
        if (eval_gate_scalar(type, flipped) != base) {
          depends |= static_cast<uint8_t>(1 << i);
        }
      }
    }
    REQUIRE(gate->relevant == depends);
    for (int i = gate->arity; i < 6; ++i) {
      REQUIRE(((depends >> i) & 1) == 0);
    }
    if (gate->arity > 0) {
      REQUIRE(((depends >> (gate->arity - 1)) & 1) == 1);
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
