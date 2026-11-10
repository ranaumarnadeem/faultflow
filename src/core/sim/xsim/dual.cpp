#include "sim/xsim/dual.hpp"

#include <stdexcept>
#include <vector>

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

constexpr int kInputs = 6;
constexpr int kMinterms = 1 << kInputs;

// A product term: the inputs in `care`, each at its bit in `value`.
struct Cube {
  uint8_t care = 0;
  uint8_t value = 0;
};

struct GateCubes {
  bool valid = false;
  std::vector<Cube> ones;   // prime implicants of the function
  std::vector<Cube> zeros;  // prime implicants of its complement
};

bool covers(const Cube& c, int m) { return (m & c.care) == c.value; }

// Every prime implicant of the minterms where `table` is `target`.
std::vector<Cube> primes(const std::vector<bool>& table, bool target) {
  const auto implicant = [&](const Cube& c) {
    for (int m = 0; m < kMinterms; ++m) {
      if (covers(c, m) && table[m] != target) {
        return false;
      }
    }
    return true;
  };
  std::vector<Cube> out;
  for (int care = 0; care < kMinterms; ++care) {
    for (int value = care;; value = (value - 1) & care) {
      const Cube c{static_cast<uint8_t>(care), static_cast<uint8_t>(value)};
      bool prime = implicant(c);
      for (int i = 0; prime && i < kInputs; ++i) {
        if ((care >> i) & 1) {
          const Cube wider{static_cast<uint8_t>(care & ~(1 << i)),
                           static_cast<uint8_t>(value & ~(1 << i))};
          prime = !implicant(wider);
        }
      }
      if (prime) {
        out.push_back(c);
      }
      if (value == 0) {
        break;
      }
    }
  }
  return out;
}

GateCubes cubes_for(GateType type) {
  GateCubes out;
  std::vector<bool> table(kMinterms);
  try {
    for (int m = 0; m < kMinterms; ++m) {
      std::vector<bool> bits(kInputs);
      for (int i = 0; i < kInputs; ++i) {
        bits[i] = ((m >> i) & 1) != 0;
      }
      table[m] = eval_gate_scalar(type, bits);
    }
  } catch (const std::exception&) {
    return out;  // no combinational function (INPUT, DFF)
  }
  out.valid = true;
  out.ones = primes(table, true);
  out.zeros = primes(table, false);
  return out;
}

const std::vector<GateCubes>& cube_table() {
  static const std::vector<GateCubes> table = [] {
    std::vector<GateCubes> t;
    for (int g = 0; g <= static_cast<int>(GateType::ICG_EN_CHAIN); ++g) {
      t.push_back(cubes_for(static_cast<GateType>(g)));
    }
    return t;
  }();
  return table;
}

uint64_t any_consistent(const std::vector<Cube>& cubes, const std::array<Dual, 6>& in) {
  uint64_t out = 0ULL;
  for (const Cube& c : cubes) {
    uint64_t term = ~0ULL;
    for (int i = 0; i < kInputs; ++i) {
      if ((c.care >> i) & 1) {
        term &= ((c.value >> i) & 1) ? in[i].h : in[i].l;
      }
    }
    out |= term;
  }
  return out;
}

}  // namespace

Dual eval_gate_dual(GateType type, const std::array<Dual, 6>& inputs) {
  const auto& table = cube_table();
  const size_t g = static_cast<size_t>(type);
  if (g >= table.size() || !table[g].valid) {
    throw std::runtime_error("eval_gate_dual: no combinational function for this gate");
  }
  return {any_consistent(table[g].ones, inputs), any_consistent(table[g].zeros, inputs)};
}

}  // namespace faultflow
