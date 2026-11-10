#include <catch2/catch_test_macros.hpp>

#include <string>
#include <vector>

#include "ir/normalized_graph/cell_map.hpp"
#include "sim/gate_eval.hpp"
#include "sim/xsim/logic.hpp"

using namespace faultflow;

namespace {

constexpr Logic L0 = Logic::ZERO;
constexpr Logic L1 = Logic::ONE;
constexpr Logic LX = Logic::X;
constexpr Logic LZ = Logic::Z;
constexpr int kMaxInputs = 6;

// Every gate type a cell map may name.
std::vector<GateType> gate_types() {
  std::vector<GateType> out;
  for (const std::string& name : gate_type_names()) {
    out.push_back(gate_type_from_name(name));
  }
  return out;
}

// The inputs a gate's two-valued function reads: one past the last input whose
// value can change its output. Derived from eval_gate_scalar, not a hand table.
int arity(GateType type) {
  int last = -1;
  for (int i = 0; i < kMaxInputs; ++i) {
    for (int m = 0; m < (1 << kMaxInputs) && last < i; ++m) {
      std::vector<bool> a(kMaxInputs);
      for (int j = 0; j < kMaxInputs; ++j) {
        a[j] = ((m >> j) & 1) != 0;
      }
      std::vector<bool> b = a;
      b[i] = !b[i];
      if (eval_gate_scalar(type, a) != eval_gate_scalar(type, b)) {
        last = i;
      }
    }
  }
  return last + 1;
}

// Each input combination over `values`, `n` inputs long.
std::vector<std::vector<Logic>> combinations(int n, const std::vector<Logic>& values) {
  std::vector<std::vector<Logic>> out(1);
  for (int i = 0; i < n; ++i) {
    std::vector<std::vector<Logic>> next;
    for (const auto& prefix : out) {
      for (Logic v : values) {
        auto c = prefix;
        c.push_back(v);
        next.push_back(c);
      }
    }
    out = std::move(next);
  }
  return out;
}

std::vector<bool> known_bits(const std::vector<Logic>& inputs) {
  std::vector<bool> out;
  for (Logic v : inputs) {
    out.push_back(v == L1);
  }
  return out;
}

// Whether some completion of the unknown inputs gives `value`.
bool some_completion_gives(GateType type, const std::vector<Logic>& inputs, bool value) {
  std::vector<size_t> unknown;
  for (size_t i = 0; i < inputs.size(); ++i) {
    if (inputs[i] == LX || inputs[i] == LZ) {
      unknown.push_back(i);
    }
  }
  for (size_t m = 0; m < (size_t{1} << unknown.size()); ++m) {
    std::vector<bool> bits = known_bits(inputs);
    for (size_t k = 0; k < unknown.size(); ++k) {
      bits[unknown[k]] = ((m >> k) & 1) != 0;
    }
    if (eval_gate_scalar(type, bits) == value) {
      return true;
    }
  }
  return false;
}

}  // namespace

TEST_CASE("X gate evaluation agrees with the two-valued table on known inputs",
          "[xsim][logic]") {
  for (GateType type : gate_types()) {
    for (const auto& in : combinations(arity(type), {L0, L1})) {
      const Logic want = eval_gate_scalar(type, known_bits(in)) ? L1 : L0;
      REQUIRE(eval_gate_x(type, in) == want);
    }
  }
}

TEST_CASE("X gate evaluation is exact: known only when every completion agrees",
          "[xsim][logic]") {
  for (GateType type : gate_types()) {
    for (const auto& in : combinations(arity(type), {L0, L1, LX})) {
      const Logic got = eval_gate_x(type, in);
      REQUIRE(got != LZ);
      const bool can0 = some_completion_gives(type, in, false);
      const bool can1 = some_completion_gives(type, in, true);
      if (can0 && can1) {
        REQUIRE(got == LX);
      } else {
        REQUIRE(got == (can1 ? L1 : L0));
      }
    }
  }
}

TEST_CASE("X gate evaluation is monotone: refining an X never flips a known output",
          "[xsim][logic]") {
  for (GateType type : gate_types()) {
    for (const auto& in : combinations(arity(type), {L0, L1, LX})) {
      const Logic got = eval_gate_x(type, in);
      for (size_t i = 0; i < in.size(); ++i) {
        if (in[i] != LX) {
          continue;
        }
        for (Logic v : {L0, L1}) {
          auto refined = in;
          refined[i] = v;
          const Logic r = eval_gate_x(type, refined);
          if (got != LX) {
            REQUIRE(r == got);
          }
        }
      }
    }
  }
}

TEST_CASE("Every cell input reads Z as X", "[xsim][logic]") {
  for (GateType type : gate_types()) {
    for (const auto& in : combinations(arity(type), {L0, L1, LX})) {
      auto as_z = in;
      for (Logic& v : as_z) {
        if (v == LX) {
          v = LZ;
        }
      }
      REQUIRE(eval_gate_x(type, as_z) == eval_gate_x(type, in));
    }
  }
}

TEST_CASE("X gate evaluation on the cases a naive model gets wrong", "[xsim][logic]") {
  // A controlling value decides the output whatever the other input is.
  REQUIRE(eval_gate_x(GateType::AND2, {L0, LX}) == L0);
  REQUIRE(eval_gate_x(GateType::AND2, {L1, LX}) == LX);
  REQUIRE(eval_gate_x(GateType::OR2, {LX, L1}) == L1);
  REQUIRE(eval_gate_x(GateType::NAND2, {LX, L0}) == L1);
  REQUIRE(eval_gate_x(GateType::INV, {LX}) == LX);
  REQUIRE(eval_gate_x(GateType::BUF, {LZ}) == LX);
  // XOR of two unknowns is unknown, though both completions of a shared X agree.
  REQUIRE(eval_gate_x(GateType::XOR2, {LX, LX}) == LX);
  REQUIRE(eval_gate_x(GateType::XOR2, {LX, L0}) == LX);
  // A mux whose data inputs agree is known whatever its select (MUX2_NI: in2 selects in1).
  REQUIRE(eval_gate_x(GateType::MUX2_NI, {L1, L1, LX}) == L1);
  REQUIRE(eval_gate_x(GateType::MUX2_NI, {L0, L0, LX}) == L0);
  REQUIRE(eval_gate_x(GateType::MUX2_NI, {L0, L1, LX}) == LX);
  // Evaluating a&~b twice, with X as 0 and as 1, says 0 for (X, X); it is X.
  REQUIRE(eval_gate_x(GateType::AND2B, {LX, LX}) == LX);
  REQUIRE(eval_gate_x(GateType::AND2B, {L0, LX}) == L0);
  // A missing input reads 0, as in the two-valued engine.
  REQUIRE(eval_gate_x(GateType::AND2, {L1}) == L0);
}

TEST_CASE("Logic helpers: not, and, or and an exact mux", "[xsim][logic]") {
  REQUIRE(logic_not(L0) == L1);
  REQUIRE(logic_not(LX) == LX);
  REQUIRE(logic_not(LZ) == LX);
  REQUIRE(logic_and(L0, LX) == L0);
  REQUIRE(logic_and(L1, LZ) == LX);
  REQUIRE(logic_or(L1, LX) == L1);
  REQUIRE(logic_or(L0, LX) == LX);
  for (Logic a : {L0, L1, LX}) {
    for (Logic b : {L0, L1, LX}) {
      REQUIRE(logic_mux(L0, a, b) == a);
      REQUIRE(logic_mux(L1, a, b) == b);
      REQUIRE(logic_mux(LX, a, b) == ((a == b && a != LX) ? a : LX));
      REQUIRE(logic_mux(LZ, a, b) == logic_mux(LX, a, b));
    }
  }
}

TEST_CASE("X clock gate agrees with eval_icg on known values", "[xsim][logic][icg]") {
  for (GateType type : {GateType::ICG_POS, GateType::ICG_NEG}) {
    for (bool clk : {false, true}) {
      for (bool en : {false, true}) {
        for (bool held : {false, true}) {
          uint64_t latch = held ? ~0ULL : 0ULL;
          const uint64_t out = eval_icg(type, clk ? ~0ULL : 0ULL, en ? ~0ULL : 0ULL, latch);
          Logic xlatch = held ? L1 : L0;
          const Logic xout = eval_icg_x(type, clk ? L1 : L0, en ? L1 : L0, xlatch);
          REQUIRE(xout == ((out & 1ULL) ? L1 : L0));
          REQUIRE(xlatch == ((latch & 1ULL) ? L1 : L0));
        }
      }
    }
  }
}

TEST_CASE("X clock gate: an unknown latch loads while the clock is off",
          "[xsim][logic][icg]") {
  // ICG_POS is transparent while the clock is low.
  Logic latch = LX;
  REQUIRE(eval_icg_x(GateType::ICG_POS, L1, L1, latch) == LX);  // on: holds X
  REQUIRE(latch == LX);
  REQUIRE(eval_icg_x(GateType::ICG_POS, L0, L1, latch) == L0);  // off: gated clock 0
  REQUIRE(latch == L1);
  REQUIRE(eval_icg_x(GateType::ICG_POS, L1, L0, latch) == L1);  // on: holds 1
  // An unknown clock: the latch keeps a value only if the enable agrees with it.
  REQUIRE(eval_icg_x(GateType::ICG_POS, LX, L1, latch) == LX);
  REQUIRE(latch == L1);
  eval_icg_x(GateType::ICG_POS, LX, L0, latch);
  REQUIRE(latch == LX);
}
